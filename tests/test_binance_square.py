import os
import re
import tempfile
import unittest
from collections import deque
from unittest import mock
from tests import _loader  # noqa: F401
from features import exchange_events as ex
from features.crypto_opportunity_detector import analyze_message

BS4 = ex.BeautifulSoup is not None


def post_block(handle, name, pid, text, with_profile=True):
    prof = (f'<a href="https://www.binance.com/en/square/profile/{handle}"><img src="x"></a>'
            f'<a href="https://www.binance.com/en/square/profile/{handle}">{name}</a>') if with_profile else ""
    return (f'<div class="card">{prof}<a href="https://www.binance.com/en/square/post/{pid}">'
            f'<div>{text}</div></a><a href="/en/square/post/{pid}">dup</a><span>12</span></div>')


def page(*blocks):
    return ('<html><body><nav><a href="/en/square">Discover</a><a href="/en/square/notifications">Notification</a></nav>'
            + "".join(blocks) + "</body></html>")


FREE = ("Learn &amp; Earn: complete the quiz and share 50,000 USDT in rewards. No deposit needed. "
        "Ends Dec 5, 2026. {future}(BNBUSDT) #LearnAndEarn")
REFERRAL = ("Airdrop alert! Claim free tokens, quiz time. Sign up with my link https://www.binance.com/activity/referral?ref=ABC12345 "
            "and use my referral code ABC12345")
BAIT = "Giveaway: like and repost to win 100 USDT airdrop, follow me and comment below to win!"
PAID = "Airdrop: deposit at least 500 USDT and stake BNB to share the prize pool"
CHAT = "Just my random thoughts about the market today, nothing special to see here at all folks"


@unittest.skipUnless(BS4, "beautifulsoup4 not installed")
class ParseTests(unittest.TestCase):
    def test_extracts_posts_authors_and_cleans(self):
        html = page(post_block("alice", "Alice", "385299644803359", FREE),
                    post_block("bob", "Bob", "385032500094953", CHAT),
                    post_block("carol", "Carol", "385032500099999", "short"))      # too short → ignored
        posts = ex.parse_square_html(html, "#airdrop")
        self.assertEqual([p["id"] for p in posts], ["square:385299644803359", "square:385032500094953"])
        a = posts[0]
        self.assertEqual(a["handle"], "alice")
        self.assertEqual(a["author"], "Alice")
        self.assertNotIn("{future}", a["summary"])
        self.assertIn("50,000 USDT", a["summary"])
        self.assertEqual(a["url"], "https://www.binance.com/en/square/post/385299644803359")
        self.assertFalse(a["official"])
        self.assertEqual(a["type_key"], "square_community")
        self.assertEqual(a["category"], "Community · #airdrop")

    def test_duplicate_links_to_same_post_collapse(self):
        posts = ex.parse_square_html(page(post_block("alice", "Alice", "385299644803359", FREE)), "x")
        self.assertEqual(len(posts), 1)

    def test_profile_page_uses_default_handle_and_official_flag(self):
        html = page(post_block("", "", "369613470990396", "Binance will host a Learn &amp; Earn quiz: share 20,000 USDC", with_profile=False))
        posts = ex.parse_square_html(html, "@binance_announcement", "binance_announcement",
                                     official=("binance_announcement",))
        self.assertTrue(posts[0]["official"])
        self.assertEqual(posts[0]["type_key"], "square_official")
        self.assertEqual(posts[0]["handle"], "binance_announcement")

    def test_official_match_is_case_insensitive(self):
        html = page(post_block("Binance_Announcement", "Binance Announcement", "369613470990396", FREE))
        self.assertTrue(ex.parse_square_html(html, "x", official=("binance_announcement",))[0]["official"])

    def test_title_is_trimmed_at_word_boundary(self):
        t = ex._square_title("word " * 60)
        self.assertLessEqual(len(t), 112)
        self.assertTrue(t.endswith("…"))

    def test_empty_and_garbage_html(self):
        self.assertEqual(ex.parse_square_html("", "x"), [])
        self.assertEqual(ex.parse_square_html("<html><body>nothing here</body></html>", "x"), [])


class FetchTests(unittest.TestCase):
    def setUp(self):
        ex._health["square"].update(pages_ok=0, pages_failed=0, page_errors=[], via_browser=0)
        p = mock.patch.object(ex.time, "sleep"); p.start(); self.addCleanup(p.stop)
        ex.SQUARE_BROWSER_FALLBACK = False

    def test_blocked_detection(self):
        self.assertTrue(ex._blocked("<html>AwsWafIntegration challenge</html>", 200))
        self.assertTrue(ex._blocked("<html>captcha</html>", 200))
        self.assertTrue(ex._blocked("ok", 405))
        self.assertTrue(ex._blocked("<html><body>empty shell</body></html>", 200))
        self.assertFalse(ex._blocked('<a href="/en/square/post/123456">x</a>', 200))

    def test_pages_built_from_config(self):
        pages, official = ex.square_pages({"square_community": True, "square_tags": ["#airdrop", "Learn Earn"],
                                           "square_accounts": ["somecreator", "binance_security"]})
        labels = [p[0] for p in pages]
        self.assertIn("@binance_announcement", labels)
        self.assertIn("@somecreator", labels)
        self.assertEqual(labels.count("@binance_security"), 1)           # no duplicate
        self.assertIn("discover", labels)
        self.assertIn("#airdrop", labels)
        self.assertIn("#LearnEarn", labels)                              # spaces removed
        self.assertIn("somecreator", official)
        pages2, _ = ex.square_pages({"square_community": False})
        self.assertTrue(all(l.startswith("@") for l, _, _ in pages2))    # official only

    @unittest.skipUnless(BS4, "beautifulsoup4 not installed")
    def test_fetch_merges_pages_and_tolerates_failures(self):
        def fake_get(url, timeout=0, headers=None):
            r = mock.Mock(status_code=200)
            if "profile/binance_announcement" in url:
                r.text = page(post_block("binance_announcement", "Binance Announcement", "400000000000002", FREE))
            elif "hashtag/airdrop" in url:
                r.text = page(post_block("alice", "Alice", "400000000000003", BAIT),
                              post_block("binance_announcement", "Binance Announcement", "400000000000002", FREE))
            elif "hashtag/giveaway" in url:
                raise ConnectionError("reset")
            else:
                r.text = page()                                         # empty page → counted as blocked/empty
                r.status_code = 200
            return r
        sess = mock.Mock(); sess.get.side_effect = fake_get
        items = ex.fetch_square(sess, {"square_tags": ["airdrop", "giveaway"], "square_community": True})
        ids = [i["id"] for i in items]
        self.assertEqual(ids, ["square:400000000000003", "square:400000000000002"])       # newest first, deduped
        self.assertTrue(next(i for i in items if i["id"].endswith("2"))["official"])      # official wins on merge
        self.assertGreater(ex._health["square"]["pages_failed"], 0)
        self.assertTrue(ex._health["square"]["page_errors"])

    def test_all_pages_failing_raises(self):
        sess = mock.Mock(); sess.get.side_effect = ConnectionError("blocked")
        with self.assertRaises(RuntimeError) as cm:
            ex.fetch_square(sess, {"square_community": False})
        self.assertIn("returned nothing", str(cm.exception))

    @unittest.skipUnless(BS4, "beautifulsoup4 not installed")
    def test_browser_fallback_used_when_challenged(self):
        ex.SQUARE_BROWSER_FALLBACK = True
        ok_html = page(post_block("binance_announcement", "BA", "400000000000009", FREE))
        sess = mock.Mock(); sess.get.return_value = mock.Mock(status_code=405, text="<html>AwsWafIntegration</html>")
        fake_browser = mock.Mock(); fake_browser.available.return_value = True; fake_browser.get.return_value = ok_html
        with mock.patch.object(ex, "_SquareBrowser", return_value=fake_browser):
            items = ex.fetch_square(sess, {"square_community": False})
        self.assertTrue(items)
        self.assertGreater(ex._health["square"]["via_browser"], 0)
        fake_browser.close.assert_called()


class GateAndDetectorTests(unittest.TestCase):
    def test_referral_and_bait_flags(self):
        a = analyze_message(REFERRAL, 0, "free")
        self.assertIn("referral_link", a["risk_flags"])
        b = analyze_message(BAIT, 0, "free")
        self.assertIn("engagement_bait", b["risk_flags"])
        clean = analyze_message(FREE.replace("&amp;", "&"), 0, "free")
        self.assertNotIn("referral_link", clean["risk_flags"])
        self.assertNotIn("engagement_bait", clean["risk_flags"])

    def test_normal_follow_phrase_is_not_bait(self):
        a = analyze_message("Follow us on X and complete the quests to join the points program Season 2", 0, "free")
        self.assertNotIn("engagement_bait", a["risk_flags"])

    def test_gate_rules(self):
        cfg = {"square_community": True}
        good = {"analysis": analyze_message(FREE.replace("&amp;", "&"), 45, "free")}
        off = {"id": "square:1", "official": True, "handle": "binance_announcement"}
        com = {"id": "square:2", "official": False, "handle": "alice"}
        self.assertTrue(ex._square_gate(off, {"analysis": {"score": 1}}, cfg, 45)[0])        # official always passes
        self.assertTrue(ex._square_gate(com, good, cfg, 45)[0])
        ref = {"analysis": analyze_message(REFERRAL, 0, "free")}
        self.assertIn("referral", ex._square_gate(com, ref, cfg, 45)[1])
        weak = {"analysis": {"score": 50, "risk_flags": []}}
        self.assertIn("needs score", ex._square_gate(com, weak, cfg, 45)[1])
        self.assertFalse(ex._square_gate(com, good, {"square_community": False}, 45)[0])
        llm_sure = {"analysis": {"score": 40, "risk_flags": []},
                    "llm": {"is_opportunity": True, "cost": "free", "confidence": 90}}
        self.assertTrue(ex._square_gate(com, llm_sure, cfg, 45)[0])

    def test_author_hourly_cap(self):
        ex._author_hits.clear()
        com = {"id": "square:2", "official": False, "handle": "Alice"}
        good = {"analysis": analyze_message(FREE.replace("&amp;", "&"), 45, "free")}
        ex._author_hits["alice"] = deque([ex.time.time()] * ex.SQUARE_AUTHOR_MAX_PER_HOUR)
        self.assertIn("this hour", ex._square_gate(com, good, {"square_community": True}, 45)[1])
        ex._author_hits["alice"] = deque([ex.time.time() - 7200] * 5)                        # old hits expire
        self.assertTrue(ex._square_gate(com, good, {"square_community": True}, 45)[0])


def sq(pid, text, handle="alice", official=False):
    return {"id": f"square:{pid}", "source": "Binance Square", "title": text[:80], "summary": text,
            "category": f"{'Official' if official else 'Community'} · #airdrop",
            "type_key": "square_official" if official else "square_community",
            "url": f"https://www.binance.com/en/square/post/{pid}", "published": 0,
            "handle": handle, "author": handle, "official": official, "square_page": "#airdrop"}


CFG = {"enabled": True, "square": True, "byx": False, "square_community": True, "mode": "intelligent", "min_score": 45,
       "cost_policy": "free", "llm_mode": "off", "include": "", "exclude": "", "prefix": "🧭 Crypto Opportunity"}
CLEAN = FREE.replace("&amp;", "&")


class PollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        ex.STATE_FILE = os.path.join(self.tmp, "state.json")
        ex._seen.clear(); ex._recent_titles.clear(); ex._recent_log.clear(); ex._author_hits.clear()
        ex._baselined.update(binance=True, bybit=True, square=False)
        ex._square_last = 0.0
        self.sent = []
        ex._cfg_provider = lambda: dict(CFG)
        ex._send = lambda text, pm=None: (self.sent.append((text, pm)) or True)

    def poll(self, square_items, **kw):
        with mock.patch.object(ex, "fetch_bybit", return_value=[]), \
             mock.patch.object(ex, "fetch_binance", return_value=[]), \
             mock.patch.object(ex, "fetch_square", return_value=list(square_items)) as fs:
            res = ex.poll_once(**kw)
        return res, fs

    def test_baseline_then_forward_official_and_good_community(self):
        self.poll([sq(100, CHAT)])                                           # baseline run: nothing sent
        self.assertEqual(self.sent, [])
        self.assertTrue(ex._baselined["square"])
        ex._square_last = 0
        res, _ = self.poll([sq(100, CHAT),
                            sq(101, CLEAN, "binance_announcement", True),
                            sq(102, CLEAN.replace("50,000", "70,000"), "alice"),
                            sq(103, REFERRAL, "spammer"),
                            sq(104, PAID, "bob"),
                            sq(105, CHAT, "carol")])
        by_id = {r["id"]: r for r in res}
        self.assertEqual(by_id["square:101"]["decision"], "forward")
        self.assertEqual(by_id["square:102"]["decision"], "skip")            # near-duplicate headline of 101
        self.assertEqual(by_id["square:103"]["decision"], "skip")
        self.assertIn("referral", by_id["square:103"]["reason"])
        self.assertEqual(by_id["square:104"]["decision"], "skip")
        self.assertEqual(by_id["square:105"]["decision"], "skip")
        self.assertEqual(len(self.sent), 1)
        text, pm = self.sent[0]
        self.assertEqual(pm, "html")
        self.assertIn("Official ✔", text)
        self.assertIn("@binance_announcement", text)

    def test_community_label_is_unverified(self):
        self.poll([sq(1, CHAT)])
        ex._square_last = 0
        self.poll([sq(2, CLEAN, "alice")])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Community (unverified)", self.sent[0][0])
        self.assertIn("@alice", self.sent[0][0])

    def test_noise_skips_not_logged_but_interesting_skips_are(self):
        self.poll([sq(1, CHAT)])
        ex._square_last = 0
        self.poll([sq(2, CHAT, "x"), sq(3, REFERRAL, "y")])
        reasons = [r["reason"] for r in ex._recent_log]
        self.assertFalse(any("no opportunity concept" in r for r in reasons))
        self.assertTrue(any("referral" in r for r in reasons))

    def test_cycle_cap_leaves_rest_unseen_for_next_cycle(self):
        self.poll([sq(1, CHAT)])
        ex._square_last = 0
        distinct = [
            "Binance Learn & Earn: complete the quiz about Sui and share 50,000 SUI in rewards. No deposit needed.",
            "Bybit Demo Trading Championship: join with demo funds, no real money, win up to 2,000 USDT",
            "Zealy quest for ProjectOrion: complete social tasks to earn points toward the retroactive airdrop, free",
            "Red Packet code BPXYZ789: claim it in the Binance app before it expires, free USDT inside",
            "Testnet is live for Atlas Network: get free test tokens from the faucet and finish Galxe quests",
            "Word of the Day on Binance: guess today's word and win a share of 500 USDT, ends Nov 30",
            "CreatorPad campaign: complete creator tasks to unlock a 250,000 GRVT voucher reward pool, free to join",
            "Wallet Booster: new user quests, complete tasks and claim free tokens before the snapshot on Dec 12",
        ]
        items = [sq(10 + i, t, f"binance_official_{i}", official=True) for i, t in enumerate(distinct)]
        self.poll(items)
        self.assertEqual(len(self.sent), ex.SQUARE_MAX_FORWARD_PER_CYCLE)           # capped
        seen = sum(1 for i in items if i["id"] in ex._seen)
        self.assertLess(seen, len(items))                                          # remainder waits…
        ex._square_last = 0
        self.poll(items)
        self.assertGreater(len(self.sent), ex.SQUARE_MAX_FORWARD_PER_CYCLE)        # …and goes out next cycle

    def test_square_polled_only_when_due(self):
        self.poll([sq(1, CHAT)])
        _, fs = self.poll([sq(1, CHAT)])
        fs.assert_not_called()                                               # just polled → not due
        ex._square_last = ex.time.time() - ex.SQUARE_POLL_INTERVAL_SEC - 1
        _, fs = self.poll([sq(1, CHAT)])
        fs.assert_called_once()

    def test_disabled_square_is_never_fetched(self):
        ex._cfg_provider = lambda: dict(CFG, square=False)
        _, fs = self.poll([sq(1, CHAT)])
        fs.assert_not_called()

    def test_hashtag_membership_gives_no_score_boost(self):
        # "Hold the new token" on an #airdrop page must not be treated as a campaign
        self.poll([sq(1, CHAT)])
        ex._square_last = 0
        res, _ = self.poll([sq(2, "Hold the new token on the exchange and watch the chart closely today", "dave")])
        self.assertEqual(res[0]["decision"], "skip")

    def test_square_failure_does_not_break_other_sources(self):
        with mock.patch.object(ex, "fetch_bybit", return_value=[]), \
             mock.patch.object(ex, "fetch_binance", return_value=[]), \
             mock.patch.object(ex, "fetch_square", side_effect=RuntimeError("bot challenge")):
            ex.poll_once()
        self.assertEqual(ex._health["square"]["fails"], 1)
        self.assertIn("bot challenge", ex._health["square"]["last_error"])

    def test_probe_reports_each_source(self):
        with mock.patch.object(ex, "fetch_bybit", return_value=[{"category": "A", "title": "t"}]), \
             mock.patch.object(ex, "fetch_binance", side_effect=RuntimeError("HTTP 403")), \
             mock.patch.object(ex, "fetch_square", return_value=[sq(1, CLEAN)]):
            out = ex.probe(cfg={})
        self.assertTrue(out["bybit"]["ok"])
        self.assertFalse(out["binance"]["ok"])
        self.assertIn("403", out["binance"]["error"])
        self.assertEqual(out["square"]["count"], 1)
        self.assertIn("playwright_available", out)


if __name__ == "__main__":
    unittest.main()
