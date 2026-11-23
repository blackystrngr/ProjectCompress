import unittest
from tests import _loader  # noqa: F401
from features.crypto_opportunity_detector import analyze_message, format_alert, format_alert_html, explain

SHOULD_ACCEPT = [
    "Binance Learn & Earn: Complete the quiz to share 50,000 USDT in token rewards! Ends Nov 30, 2026.",
    "New Learn and Earn campaign for SUI is live. Watch the lesson, answer the quiz, and share 120,000 SUI.",
    "Word of the Day (WOTD): guess today's word and win a share of 500 USDT in rewards",
    "🎁 Binance Red Packet code: BPABC123 — claim it in your Binance app before it expires",
    "Bybit Demo Trading Championship: join with demo funds, no real money needed, win up to 2,000 USDT",
    "Crypto Box giveaway! Enter the code to claim free USDT. First come, first served.",
    "New project X testnet is live. Get free test tokens from the faucet and complete tasks on Galxe.",
    "ProjectY airdrop checker is live! Check eligibility and claim your tokens before the snapshot on Dec 5.",
    "No deposit required: complete KYC and get a 10 USDT voucher. Limited to the first 5,000 users.",
    "Staking is not required. Just follow us on X and complete the quests to join the points program Season 2.",
    "Zero deposit promo: sign up, verify your account and receive 5 USDT bonus",
    "Join the Zealy quest — complete the social tasks to earn points toward the retroactive airdrop.",
    "Deposits and withdrawals are open. Also: free airdrop for early users, claim now. No purchase necessary.",
    "Airdrop live, claim free tokens. You don't need to deposit or stake anything.",
]
SHOULD_REJECT = [
    "Deposit 100 USDT and get a 10 USDT bonus airdrop!",
    "Stake at least 500 BNB to participate in the Launchpool and earn rewards",
    "Trade $10,000 in futures volume to share a 100,000 USDT prize pool",
    "Hold at least 1,000 tokens to be eligible for the airdrop",
    "Buy the token and bridge to Arbitrum to qualify for the airdrop",
    "Requirements:\n- Deposit 50 USDT\n- Join the airdrop campaign",
    "Binance HODLer Airdrops: subscribe BNB to Simple Earn to share the new token airdrop",
    "Megadrop: lock BNB to earn points and airdrop rewards",
    "CLAIM YOUR AIRDROP: connect your wallet to verify and enter your seed phrase",
    "Binance airdrop live! Claim at https://binance-airdrop-claim.xyz/now",
    "Send 0.1 ETH to claim your 5 ETH airdrop",
    "Guaranteed profit! Double your BTC in 24 hours, DM me to claim",
    "Binance will list XYZ on spot. Deposits open now, trading starts 2026-12-01.",
    "Scheduled system upgrade: wallet maintenance on Nov 28.",
    "Bitcoin price analysis: BTC consolidates near resistance",
]


class DetectorTests(unittest.TestCase):
    def test_accepts_real_free_opportunities(self):
        for t in SHOULD_ACCEPT:
            with self.subTest(t=t[:60]):
                a = analyze_message(t, 45, "free")
                self.assertTrue(a["accepted"], f"{a['score']} {a['reason']}")

    def test_rejects_paid_scam_and_noise(self):
        for t in SHOULD_REJECT:
            with self.subTest(t=t[:60]):
                a = analyze_message(t, 45, "free")
                self.assertFalse(a["accepted"], f"{a['score']} {a['reason']}")

    def test_original_include_vocabulary_is_covered(self):
        """Every term from the legacy include regex must be recognised (v1 dropped them)."""
        for t in ["learn & earn campaign", "read to earn", "demo trading contest", "demo championship",
                  "demo arena", "demo challenge", "no deposit bonus", "zero deposit promo", "quiz time",
                  "free usdt giveaway", "red packet", "word of the day", "wotd"]:
            with self.subTest(t=t):
                a = analyze_message(t, 0, "free")
                self.assertNotEqual(a["category"], "unknown")

    def test_negation_is_understood(self):
        a = analyze_message("Airdrop live. No deposit needed, no staking, claim free tokens now.", 45, "free")
        self.assertTrue(a["accepted"])
        self.assertEqual(a["cost_tier"], "free")
        self.assertEqual(a["cost_evidence"]["required"], [])

    def test_requirement_overrides_negation_elsewhere(self):
        a = analyze_message("Airdrop live. No deposit needed, but stake 100 USDT to double your points", 45, "free")
        self.assertFalse(a["accepted"])
        self.assertIn("stake", a["cost_evidence"]["required"])

    def test_policy_any_ignores_cost_but_not_scams(self):
        self.assertTrue(analyze_message("Deposit 100 USDT and get a 10 USDT bonus airdrop!", 40, "any")["accepted"])
        self.assertFalse(analyze_message("Airdrop: enter your seed phrase to claim", 0, "any")["accepted"])

    def test_strict_policy_needs_explicit_free(self):
        t = "ProjectY airdrop checker is live! Check eligibility and claim your tokens before the snapshot."
        self.assertTrue(analyze_message(t, 45, "free")["accepted"])
        self.assertFalse(analyze_message(t, 45, "strict")["accepted"])

    def test_extraction(self):
        a = analyze_message("Learn & Earn: share 50,000 USDT. Ends Nov 30, 2026.", 45, "free")
        self.assertIn("50,000", a["reward"])
        self.assertIn("Nov 30", a["deadline"])

    def test_exchange_context_hint_adds_signal(self):
        t = "Hold the new token on the exchange"
        self.assertEqual(analyze_message(t, 45, "free")["category"], "unknown")
        self.assertNotEqual(analyze_message(t, 0, "free", context_hint="Latest Activities")["category"], "unknown")

    def test_borderline_is_flagged_for_review(self):
        a = analyze_message("Season 3 is coming soon", 45, "free")
        self.assertFalse(a["accepted"])
        self.assertTrue(a["needs_review"])

    def test_hard_rejects_are_never_reviewable(self):
        a = analyze_message("Airdrop: enter your seed phrase", 0, "free")
        self.assertTrue(a.get("hard_reject"))
        self.assertFalse(a["needs_review"])

    def test_empty_and_unicode_noise(self):
        self.assertFalse(analyze_message("", 45)["accepted"])
        self.assertTrue(analyze_message("𝗟𝗲𝗮𝗿𝗻 & 𝗘𝗮𝗿𝗻: 𝗰𝗼𝗺𝗽𝗹𝗲𝘁𝗲 𝘁𝗵𝗲 𝗾𝘂𝗶𝘇 and share 5,000 USDT", 45)["accepted"])

    def test_alert_formats_are_safe(self):
        a = analyze_message("Quiz <b>time</b> & win 100 USDT", 0, "free")
        plain = format_alert("Quiz <b>time</b>", a, "chan", "https://t.me/x/1")
        self.assertIn("UNVERIFIED", plain)
        h = format_alert_html("Quiz <script>x</script>", a, "chan <i>", "https://t.me/x/1", prefix="P")
        self.assertNotIn("<script>", h)
        self.assertIn("&lt;script&gt;", h)
        self.assertTrue(explain(a))


if __name__ == "__main__":
    unittest.main()
