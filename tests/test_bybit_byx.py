import os
import tempfile
import unittest
from collections import deque
from unittest import mock
from tests import _loader  # noqa: F401
from features import exchange_events as ex
from features.crypto_opportunity_detector import analyze_message

BS4 = ex.BeautifulSoup is not None
OFFICIAL_AVATAR = "https://s1.bycsi.com/user/avatars/official_cus_x2.png"


def post(uid, name, when, body, avatar="https://s1.bycsi.com/user/avatars/default20.png", mood="",
         chip="SOLUSDT", tags=("#ByXIMPACT",), likes=3):
    """Markup shaped like the real ByX feed: avatar link, name link, time, body, chips, counters."""
    chips = f'<a href="https://www.bybit.com/en/trade/spot/SOL/USDT">{chip}</a>' if chip else ""
    tag_html = "".join(f"<span>{t}</span>" for t in tags)
    return (f'<div class="card"><a href="https://www.bybit.com/en/social/user/{uid}"><img src="{avatar}"></a>'
            f'<div><a href="https://www.bybit.com/en/social/user/{uid}">{name}</a>'
            f'<span>{when}</span><span>{mood}</span></div>'
            f'<div>{body}</div><img alt="Post image 1" src="x.png">{chips}{tag_html}'
            f'<div><img alt="like"><span>{likes}</span><img alt="comment"><span></span><img alt="repost">'
            f'<img alt="share"><span>5</span></div></div>')


def page(*blocks):
    return ('<html><body><nav><a href="/en/markets">Markets</a></nav>' + "".join(blocks) + "</body></html>")


QUIZ = ("Bybit Learn & Earn: complete the quiz and share 20,000 USDT in rewards. No deposit needed. Ends Dec 5, 2026.")
CHAT = "SOL is approaching a level where the next move could reveal whether buyers are ready for another leg higher"
TRADE = "Deposit 500 USDT and trade futures to share a 10,000 USDT prize pool, stake BNB to qualify"
REF = "Free airdrop quiz! Sign up with my link https://www.bybit.com/register?ref=ABC12345 and my referral code ABC12345"
CFG = {"enabled": True, "square": False, "byx": True, "square_community": True, "mode": "intelligent",
       "min_score": 45, "cost_policy": "free", "llm_mode": "off", "include": "", "exclude": "",
       "prefix": "🧭 Crypto Opportunity"}


@unittest.skipUnless(BS4, "beautifulsoup4 not installed")
class ParseTests(unittest.TestCase):
    def test_extracts_author_time_body_and_drops_ui_noise(self):
        html = page(post("abc123", "ChaseMaster", "03:30 26 thg 11, 2026", CHAT, mood="Bullish"))
        p = ex.parse_byx_html(html, "feed")[0]
        self.assertEqual(p["author"], "ChaseMaster")
        self.assertEqual(p["handle"], "abc123")
        self.assertEqual(p["posted"], "03:30 26 thg 11, 2026")
        self.assertEqual(p["summary"], CHAT + " #ByXIMPACT")        # hashtag kept once; chip/counters dropped
        self.assertNotIn("SOLUSDT", p["summary"])
        self.assertNotIn("Bullish", p["summary"])
        self.assertEqual(p["url"], "https://www.bybit.com/en/social/user/abc123")
        self.assertEqual(p["type_key"], "byx_community")
        self.assertFalse(p["official"])

    def test_official_by_avatar_and_by_id(self):
        html = page(post("kxobMj", "BybitX", "13:46 25 Nov 2026", QUIZ, avatar=OFFICIAL_AVATAR, chip=None),
                    post("zzz999", "Somebody", "13:40 25 Nov 2026", QUIZ + " (copy)", chip=None))
        a, b = ex.parse_byx_html(html, "feed", official_ids=("kxobMj",))
        self.assertTrue(a["official"]); self.assertEqual(a["type_key"], "byx_official")
        self.assertFalse(b["official"])
        # avatar alone is enough even if the id is not configured
        c = ex.parse_byx_html(page(post("newid1", "BybitX 2", "1:00", QUIZ, avatar=OFFICIAL_AVATAR, chip=None)), "feed")[0]
        self.assertTrue(c["official"])

    def test_identical_repost_spam_collapses_and_ids_are_stable(self):
        spam = "XRP market update: bearish, support 0.895 resistance 0.945, trading strategy long short outlook"
        html = page(post("bot1", "pan***@****", "03:25 26 Nov", spam, chip=None, tags=()),
                    post("bot1", "pan***@****", "03:24 26 Nov", spam, chip=None, tags=()))
        posts = ex.parse_byx_html(html, "feed")
        self.assertEqual(len(posts), 1)
        again = ex.parse_byx_html(html, "feed")
        self.assertEqual(posts[0]["id"], again[0]["id"])
        other = ex.parse_byx_html(page(post("bot2", "x", "03:25", spam, chip=None, tags=())), "feed")
        self.assertNotEqual(posts[0]["id"], other[0]["id"])                # same text, other author → other id

    def test_short_posts_ignored_and_garbage_is_safe(self):
        self.assertEqual(ex.parse_byx_html(page(post("a", "A", "1:00", "gm", chip=None, tags=())), "feed"), [])
        self.assertEqual(ex.parse_byx_html("", "feed"), [])
        self.assertEqual(ex.parse_byx_html("<html><body>nothing</body></html>", "feed"), [])

    def test_avatar_and_name_links_belong_to_one_block(self):
        posts = ex.parse_byx_html(page(post("u1", "Alice", "2:00", CHAT), post("u2", "Bob", "2:01", CHAT + " again")), "feed")
        self.assertEqual([p["author"] for p in posts], ["Alice", "Bob"])


class FetchTests(unittest.TestCase):
    def setUp(self):
        ex._health["byx"].update(pages_ok=0, pages_failed=0, page_errors=[], via_browser=0)
        p = mock.patch.object(ex.time, "sleep"); p.start(); self.addCleanup(p.stop)
        ex.SQUARE_BROWSER_FALLBACK = False

    def test_blocked_uses_byx_markers(self):
        self.assertTrue(ex._blocked("<html>just a shell</html>", 200, ex._BYX_MARKERS))
        self.assertFalse(ex._blocked('<a href="/en/social/user/abc">x</a>', 200, ex._BYX_MARKERS))
        self.assertTrue(ex._blocked('<a href="/en/social/user/abc">x</a>', 403, ex._BYX_MARKERS))

    def test_pages_from_config(self):
        pages, ids = ex.byx_pages({"square_community": True, "byx_accounts": ["xyz", "kxobMj"]})
        labels = [p[0] for p in pages]
        self.assertEqual(labels[0], "feed")
        self.assertIn("@xyz", labels)
        self.assertEqual(labels.count("@kxobMj"), 1)
        pages2, _ = ex.byx_pages({"square_community": False})
        self.assertTrue(all(l.startswith("@") for l, _, _ in pages2))

    @unittest.skipUnless(BS4, "beautifulsoup4 not installed")
    def test_fetch_merges_and_tolerates_failures(self):
        feed = page(post("u1", "Alice", "2:00", CHAT), post("kxobMj", "BybitX", "2:01", QUIZ, avatar=OFFICIAL_AVATAR, chip=None))
        prof = page(post("kxobMj", "BybitX", "2:01", QUIZ, avatar=OFFICIAL_AVATAR, chip=None))

        def fake_get(url, timeout=0, headers=None):
            if url.endswith("/user/xyz"):
                raise ConnectionError("reset")
            return mock.Mock(status_code=200, text=prof if "/user/" in url else feed)
        sess = mock.Mock(); sess.get.side_effect = fake_get
        items = ex.fetch_byx(sess, {"square_community": True, "byx_accounts": ["xyz"]})
        self.assertEqual(len(items), 2)                                      # merged + deduped
        self.assertTrue(next(i for i in items if i["author"] == "BybitX")["official"])
        self.assertEqual(ex._health["byx"]["pages_failed"], 1)

    def test_all_failing_raises(self):
        sess = mock.Mock(); sess.get.side_effect = ConnectionError("blocked")
        with self.assertRaises(RuntimeError) as cm:
            ex.fetch_byx(sess, {"square_community": False})
        self.assertIn("returned nothing", str(cm.exception))


def byx(uid, name, text, official=False):
    return {"id": f"byx:{abs(hash((uid, text))) % 10**12:012d}", "source": "Bybit ByX", "title": text[:80], "summary": text,
            "category": f"{'Official' if official else 'Community'} · feed",
            "type_key": "byx_official" if official else "byx_community",
            "url": f"https://www.bybit.com/en/social/user/{uid}", "published": 0, "posted": "now",
            "handle": uid, "author": name, "official": official, "square_page": "feed"}


class PollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        ex.STATE_FILE = os.path.join(self.tmp, "state.json")
        ex._seen.clear(); ex._recent_titles.clear(); ex._recent_log.clear(); ex._author_hits.clear()
        ex._baselined.update(binance=True, bybit=True, square=True, byx=False)
        ex._square_last = ex.time.time(); ex._byx_last = 0.0
        self.sent = []
        ex._cfg_provider = lambda: dict(CFG)
        ex._send = lambda text, pm=None: (self.sent.append((text, pm)) or True)

    def poll(self, items, cfg=None, **kw):
        if cfg:
            ex._cfg_provider = lambda: dict(cfg)
        with mock.patch.object(ex, "fetch_bybit", return_value=[]), \
             mock.patch.object(ex, "fetch_binance", return_value=[]), \
             mock.patch.object(ex, "fetch_square", return_value=[]), \
             mock.patch.object(ex, "fetch_byx", return_value=list(items)) as fb:
            res = ex.poll_once(**kw)
        return res, fb

    def test_baseline_then_forward_official_and_good_community(self):
        self.poll([byx("u1", "Alice", CHAT)])
        self.assertEqual(self.sent, [])
        ex._byx_last = 0
        res, _ = self.poll([byx("u1", "Alice", CHAT),
                            byx("kxobMj", "BybitX", QUIZ, official=True),
                            byx("spam", "Spammer", REF),
                            byx("bob", "Bob", TRADE),
                            byx("carol", "Carol", CHAT + " different words entirely today")])
        decisions = {r["author"]: (r["decision"], r["reason"]) for r in res}
        self.assertEqual(decisions["BybitX"][0], "forward")
        self.assertIn("referral", decisions["Spammer"][1])
        self.assertEqual(decisions["Bob"][0], "skip")
        self.assertEqual(decisions["Carol"][0], "skip")
        self.assertEqual(len(self.sent), 1)
        text, pm = self.sent[0]
        self.assertEqual(pm, "html")
        self.assertIn("Bybit ByX · Official ✔ · BybitX", text)
        self.assertIn("https://www.bybit.com/en/social/user/kxobMj", text)

    def test_community_post_labelled_unverified(self):
        self.poll([byx("u1", "Alice", CHAT)])
        ex._byx_last = 0
        good = "Quiz time: complete the Learn & Earn quiz and share 5,000 USDT, no deposit needed. Ends Dec 9, 2026."
        self.poll([byx("u9", "NiceUser", good)])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Community (unverified) · NiceUser", self.sent[0][0])

    def test_byx_and_square_author_caps_are_independent(self):
        it_b = {"type_key": "byx_community", "handle": "Same"}
        it_s = {"type_key": "square_community", "handle": "Same"}
        self.assertNotEqual(ex._author_key(it_b), ex._author_key(it_s))
        ex._author_hits[ex._author_key(it_b)] = deque([ex.time.time()] * ex.SQUARE_AUTHOR_MAX_PER_HOUR)
        self.assertFalse(ex._author_allowed(ex._author_key(it_b)))
        self.assertTrue(ex._author_allowed(ex._author_key(it_s)))

    def test_cadence_and_toggle(self):
        _, fb = self.poll([byx("u1", "Alice", CHAT)])                   # due (byx_last=0)
        fb.assert_called_once()
        _, fb = self.poll([byx("u1", "Alice", CHAT)])                   # just polled
        fb.assert_not_called()
        ex._byx_last = 0
        _, fb = self.poll([byx("u1", "Alice", CHAT)], cfg=dict(CFG, byx=False))
        fb.assert_not_called()

    def test_byx_failure_is_isolated(self):
        with mock.patch.object(ex, "fetch_bybit", return_value=[]), mock.patch.object(ex, "fetch_binance", return_value=[]), \
             mock.patch.object(ex, "fetch_square", return_value=[]), \
             mock.patch.object(ex, "fetch_byx", side_effect=RuntimeError("HTTP 403")):
            ex.poll_once()
        self.assertEqual(ex._health["byx"]["fails"], 1)
        self.assertIn("403", ex._health["byx"]["last_error"])

    def test_probe_includes_byx(self):
        with mock.patch.object(ex, "fetch_bybit", return_value=[]), mock.patch.object(ex, "fetch_binance", return_value=[]), \
             mock.patch.object(ex, "fetch_square", return_value=[]), \
             mock.patch.object(ex, "fetch_byx", return_value=[byx("u1", "Alice", CHAT)]):
            out = ex.probe(cfg={})
        self.assertTrue(out["byx"]["ok"]); self.assertEqual(out["byx"]["count"], 1)
        self.assertIn("byx_pages", out)


class DetectorTests(unittest.TestCase):
    def test_post_to_earn_campaign_hashtag_is_a_promo_signal(self):
        a = analyze_message("Post with #WSOTShowYourAITradeToEarn10USDT and let the community learn from your setup", 0, "free")
        self.assertNotEqual(a["category"], "unknown")
        b = analyze_message("Share your tip to earn 10 USDT. Join the campaign, no deposit needed.", 0, "free")
        self.assertNotEqual(b["category"], "unknown")

    def test_plain_market_analysis_is_still_ignored(self):
        a = analyze_message(CHAT + ". #ByXIMPACT $SOLUSDT", 45, "free")
        self.assertFalse(a["accepted"])


if __name__ == "__main__":
    unittest.main()
