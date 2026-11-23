import os
import re
import tempfile
import unittest
from unittest import mock
from tests import _loader  # noqa: F401
from features import exchange_events as ex

CFG = {"enabled": True, "exchange": True, "mode": "intelligent", "min_score": 45, "cost_policy": "free",
       "llm_mode": "off", "include": r"\bquiz\b", "exclude": r"\bdelisting notice\b",
       "prefix": "🧭 Crypto Opportunity"}


def item(i, title, source="Bybit", category="Latest Activities", summary="", code=None):
    return {"id": f"{source.lower()}:{i}", "source": source, "title": title, "summary": summary,
            "category": category, "type_key": "", "url": f"https://x/{i}", "published": 1700000000000 + i,
            **({"code": code} if code else {})}


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        ex.STATE_FILE = os.path.join(self.tmp, "state.json")
        ex._seen.clear(); ex._recent_titles.clear(); ex._recent_log.clear()
        ex._baselined.update(binance=False, bybit=False)
        self.sent = []
        ex._cfg_provider = lambda: dict(CFG)
        ex._send = lambda text, pm=None: (self.sent.append((text, pm)) or True)

    def run_poll(self, bybit=(), binance=(), detail=""):
        with mock.patch.object(ex, "fetch_bybit", return_value=list(bybit)), \
             mock.patch.object(ex, "fetch_binance", return_value=list(binance)), \
             mock.patch.object(ex, "binance_detail", return_value=detail):
            return ex.poll_once()

    def test_first_run_is_baseline_only(self):
        self.run_poll(bybit=[item(1, "Learn & Earn: quiz and share 5,000 USDT")])
        self.assertEqual(self.sent, [])
        self.assertTrue(ex._baselined["bybit"])

    def test_new_free_event_forwarded_once_with_html(self):
        self.run_poll(bybit=[item(1, "old")], binance=[item(1, "old", "Binance")])
        new = item(2, "Learn & Earn: complete the quiz and share 5,000 USDT, ends Nov 30")
        self.run_poll(bybit=[item(1, "old"), new], binance=[item(1, "old", "Binance")])
        self.run_poll(bybit=[item(1, "old"), new], binance=[item(1, "old", "Binance")])
        self.assertEqual(len(self.sent), 1)
        text, pm = self.sent[0]
        self.assertEqual(pm, "html")
        self.assertIn("5,000", text)

    def test_paid_event_skipped_and_logged(self):
        self.run_poll(bybit=[item(1, "old")], binance=[item(1, "old", "Binance")])
        self.run_poll(bybit=[item(1, "old"), item(2, "Deposit 100 USDT to share a 50,000 USDT prize pool")],
                      binance=[item(1, "old", "Binance")])
        self.assertEqual(self.sent, [])
        self.assertEqual(ex._recent_log[-1]["decision"], "skip")
        self.assertIn("capital", ex._recent_log[-1]["reason"])

    def test_binance_body_can_flip_the_decision(self):
        self.run_poll(bybit=[item(1, "old")], binance=[item(1, "old", "Binance")])
        b = item(5, "Binance Airdrop Campaign: claim free tokens", "Binance", "Crypto Airdrop", code="abc")
        self.run_poll(bybit=[item(1, "old")], binance=[item(1, "old", "Binance"), b],
                      detail="To qualify you must deposit at least 100 USDT and hold BNB.")
        self.assertEqual(self.sent, [])

    def test_already_forwarded_from_telegram_is_skipped(self):
        self.run_poll(bybit=[item(1, "old")], binance=[item(1, "old", "Binance")])
        ex.note_forwarded("Learn & Earn: complete the quiz and share 5,000 USDT\nmore text")
        self.run_poll(bybit=[item(1, "old"), item(2, "Learn & Earn: complete the quiz and share 5,000 USDT")],
                      binance=[item(1, "old", "Binance")])
        self.assertEqual(self.sent, [])
        self.assertIn("telegram", ex._recent_log[-1]["reason"].lower())

    def test_not_ready_keeps_item_for_next_cycle(self):
        self.run_poll(bybit=[item(1, "old")], binance=[item(1, "old", "Binance")])
        ex._send = None
        new = item(2, "Learn & Earn: complete the quiz and share 5,000 USDT")
        self.run_poll(bybit=[item(1, "old"), new], binance=[item(1, "old", "Binance")])
        self.assertNotIn(new["id"], ex._seen)
        ex._send = lambda text, pm=None: (self.sent.append((text, pm)) or True)
        self.run_poll(bybit=[item(1, "old"), new], binance=[item(1, "old", "Binance")])
        self.assertEqual(len(self.sent), 1)

    def test_state_persists(self):
        self.run_poll(bybit=[item(1, "old")], binance=[item(1, "old", "Binance")])
        ex._seen.clear(); ex._baselined.update(binance=False, bybit=False)
        ex._load_state()
        self.assertTrue(ex._baselined["bybit"])
        self.assertIn("bybit:1", ex._seen)

    def test_fetch_failure_is_contained(self):
        with mock.patch.object(ex, "fetch_bybit", side_effect=RuntimeError("blocked")), \
             mock.patch.object(ex, "fetch_binance", side_effect=RuntimeError("403")):
            ex.poll_once()
        self.assertEqual(ex._health["bybit"]["fails"], 1)
        self.assertIn("blocked", ex._health["bybit"]["last_error"])

    def test_parsers_on_documented_shapes(self):
        bybit_payload = {"retCode": 0, "result": {"list": [{
            "title": "Bybit Learn & Earn: quiz", "description": "Win USDT", "type": {"title": "Latest Activities", "key": "latest_activities"},
            "tags": ["Rewards"], "url": "https://announcements.bybit.com/en/article/x", "dateTimestamp": 1, "publishTime": 1700000000000}]}}
        fake = mock.Mock(status_code=200); fake.json.return_value = bybit_payload
        with mock.patch.object(ex, "_get", return_value=fake):
            items = ex.fetch_bybit(mock.Mock())
        self.assertEqual(items[0]["category"], "Latest Activities · Rewards")
        self.assertTrue(items[0]["id"].startswith("bybit:"))
        arts = ex._binance_articles_from_json({"data": {"catalogs": [{"articles": [{"code": "c1", "title": "t"}]}]}})
        self.assertEqual(arts[0]["code"], "c1")
        txt = []
        ex._collect_text({"node": "root", "child": [{"text": "Hello"}, {"child": [{"text": "World"}]}]}, txt)
        self.assertEqual(" ".join(txt), "Hello World")

    def test_title_overlap_registry(self):
        ex.note_forwarded("Binance Learn & Earn: complete the quiz and share 50,000 USDT in rewards")
        self.assertTrue(ex.covered("Learn & Earn quiz share 50,000 USDT rewards binance complete"))
        self.assertFalse(ex.covered("Totally different headline about something else entirely"))


if __name__ == "__main__":
    unittest.main()
