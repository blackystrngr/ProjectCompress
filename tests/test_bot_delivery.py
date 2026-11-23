import unittest
from unittest import mock
from tests import _loader  # noqa: F401
from features import bot_delivery as bd

TOKEN = "123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-RU"
CFG = {"delivery": "bot", "bot_token": TOKEN, "bot_chat": "555"}


def resp(status, data):
    return (status, data)


class BotDeliveryTests(unittest.TestCase):
    def test_is_configured(self):
        self.assertTrue(bd.is_configured(CFG))
        self.assertFalse(bd.is_configured(dict(CFG, delivery="account")))
        self.assertFalse(bd.is_configured(dict(CFG, bot_token="nope")))
        self.assertFalse(bd.is_configured(dict(CFG, bot_chat="")))

    def test_send_ok_builds_correct_request(self):
        with mock.patch.object(bd, "_call", return_value=resp(200, {"ok": True})) as c:
            ok, err = bd.send("<b>hi</b>", "html", CFG)
        self.assertTrue(ok)
        token, method, payload = c.call_args.args[0], c.call_args.args[1], c.call_args.args[2]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(payload["chat_id"], "555")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertTrue(payload["link_preview_options"]["is_disabled"])
        self.assertNotIn("disable_notification", payload)          # must notify

    def test_parse_error_retries_as_plain_text(self):
        seq = [resp(400, {"description": "Bad Request: can't parse entities: Unsupported start tag"}),
               resp(200, {"ok": True})]
        with mock.patch.object(bd, "_call", side_effect=seq) as c:
            ok, _ = bd.send("<b>hi</b> &amp; bye", "html", CFG)
        self.assertTrue(ok)
        second = c.call_args_list[1].args[2]
        self.assertNotIn("parse_mode", second)
        self.assertEqual(second["text"], "hi & bye")

    def test_rate_limit_waits_and_retries(self):
        seq = [resp(429, {"description": "Too Many Requests", "parameters": {"retry_after": 2}}),
               resp(200, {"ok": True})]
        with mock.patch.object(bd, "_call", side_effect=seq), mock.patch.object(bd.time, "sleep") as sl:
            ok, _ = bd.send("x", None, CFG)
        self.assertTrue(ok)
        sl.assert_called_with(2)

    def test_actionable_errors(self):
        cases = [(403, "Forbidden: bot can't initiate conversation with a user", "press START"),
                 (400, "Bad Request: chat not found", "Detect my chat"),
                 (401, "Unauthorized", "token rejected"),
                 (403, "Forbidden: bot was blocked by the user", "press START"),
                 (400, "Bad Request: not enough rights to send text messages to the chat", "admin")]
        for status, desc, hint in cases:
            with self.subTest(desc=desc), mock.patch.object(bd, "_call", return_value=resp(status, {"description": desc})):
                ok, err = bd.send("x", None, CFG)
                self.assertFalse(ok)
                self.assertIn(hint.lower(), err.lower())

    def test_network_error_retries_then_fails(self):
        with mock.patch.object(bd, "_call", return_value=resp(0, {"description": "ConnectionError"})) as c, \
             mock.patch.object(bd.time, "sleep"):
            ok, err = bd.send("x", None, CFG)
        self.assertFalse(ok)
        self.assertEqual(c.call_count, 3)

    def test_token_never_leaks_in_errors(self):
        with mock.patch.object(bd.requests, "post", side_effect=RuntimeError(f"boom https://api.telegram.org/bot{TOKEN}/x")):
            status, data = bd._call(TOKEN, "getMe")
        self.assertEqual(status, 0)
        self.assertNotIn(TOKEN, data["description"])

    def test_long_text_is_clipped(self):
        with mock.patch.object(bd, "_call", return_value=resp(200, {"ok": True})) as c:
            bd.send("a" * 9000, None, CFG)
        self.assertLessEqual(len(c.call_args.args[2]["text"]), 4096)

    def test_verify(self):
        with mock.patch.object(bd, "_call", return_value=resp(200, {"ok": True, "result": {"username": "my_alert_bot", "first_name": "Alerts", "id": 9}})):
            self.assertEqual(bd.verify(TOKEN)["username"], "my_alert_bot")
        self.assertFalse(bd.verify("garbage")["ok"])
        with mock.patch.object(bd, "_call", return_value=resp(401, {"description": "Unauthorized"})):
            self.assertFalse(bd.verify(TOKEN)["ok"])

    def test_detect_prefers_private_chat_newest_first(self):
        updates = {"ok": True, "result": [
            {"update_id": 1, "channel_post": {"chat": {"id": -100123, "type": "channel", "title": "News"}}},
            {"update_id": 2, "message": {"chat": {"id": 777, "type": "private", "first_name": "Kisal", "last_name": "P"}}},
            {"update_id": 3, "message": {"chat": {"id": 777, "type": "private", "first_name": "Kisal"}}}]}
        with mock.patch.object(bd, "_call", return_value=resp(200, updates)):
            r = bd.detect_chats(TOKEN)
        self.assertEqual(r["chats"][0]["id"], 777)
        self.assertEqual(r["chats"][0]["type"], "private")
        self.assertEqual(len(r["chats"]), 2)

    def test_detect_empty_and_webhook(self):
        with mock.patch.object(bd, "_call", return_value=resp(200, {"ok": True, "result": []})):
            r = bd.detect_chats(TOKEN)
        self.assertEqual(r["chats"], []); self.assertIn("START", r["error"])
        with mock.patch.object(bd, "_call", return_value=resp(409, {"description": "Conflict: can't use getUpdates method while webhook is active"})):
            self.assertIn("webhook", bd.detect_chats(TOKEN)["error"])


if __name__ == "__main__":
    unittest.main()
