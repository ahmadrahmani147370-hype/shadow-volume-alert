"""Offline tests: Telegram command handling with mocked Telegram responses (no token, no network).

Run from the project folder:  python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shadow_volume_alert as sva  # noqa: E402

CHAT_ID = "123456"


class FakeResponse:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class FakeSession:
    """Records Telegram calls and returns queued responses per method."""

    def __init__(self):
        self.calls = []
        self.queue = {}

    def respond(self, method, status, data):
        self.queue.setdefault(method, []).append((status, data))

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        if self.queue.get(method):
            return FakeResponse(*self.queue[method].pop(0))
        return FakeResponse(200, {"ok": True, "result": True})

    def get(self, url, timeout=None):
        return self.post(url, timeout=timeout)

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]


def make_cfg():
    cfg = sva.load_config("/nonexistent")
    cfg.update(token="TEST", chat_id=CHAT_ID)
    return cfg


def update(uid, text, chat_id=CHAT_ID, date=None, kind="message"):
    return {"update_id": uid, kind: {"message_id": uid * 10, "date": int(date or time.time()),
                                     "chat": {"id": int(chat_id), "type": "private"}, "text": text}}


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        sva.STATE_FILE = os.path.join(self.tmp, "state.json")
        self.cfg = make_cfg()
        self.state = sva.empty_state()
        self.session = FakeSession()
        self.tg = sva.Telegram("TEST", CHAT_ID, False, session=self.session)
        self.runtime = {"started": time.time() - 3700, "scans": 12, "last_scan": time.time() - 60,
                        "last_scan_pools": 150, "next_scan": time.time() + 240, "scanning": False}

    def cmd(self, text):
        return sva.handle_command(text, self.cfg, self.state, self.runtime)

    def test_start_help_settings(self):
        self.assertIn("SHADOW VOLUME ALERT", self.cmd("/start"))
        self.assertIn("$10.0K", self.cmd("/start"))
        helptext = self.cmd("/help")
        for c, _ in sva.BOT_COMMANDS:
            self.assertIn("/" + c, helptext)
        settings = self.cmd("/settings")
        self.assertIn("2.50x", settings)
        self.assertIn("Cooldown per coin: 1h 0m", settings)
        self.assertIn("Skip high-risk tokens: no", settings)

    def test_status(self):
        sva.record_alert(self.state, {"pair": "P1", "mint": "M1", "name": "Doge<", "symbol": "DG", "vol5m": 15000,
                                      "liquidity": 30000, "url": "https://x"}, 3.1, time.time(), None)
        self.state["readings"] = {"a": {}, "b": {}}
        s = self.cmd("/status")
        self.assertIn("Scans done: 12", s)
        self.assertIn("Uptime: 1h 1m", s)
        self.assertIn("Pools tracked: 2", s)
        self.assertIn("Alerts today: 1", s)
        self.assertIn("DG 3.10x", s)
        self.assertIn("Next scan: in 3m", s)
        self.runtime["scanning"] = True
        self.assertIn("scanning now", self.cmd("/status"))

    def test_last(self):
        self.assertEqual(self.cmd("/last"), "No alerts yet.")
        for i in range(7):
            sva.record_alert(self.state, {"pair": "P%d" % i, "mint": "M%d" % i, "name": "Coin%d" % i,
                                          "symbol": "C%d" % i, "vol5m": 1e4, "liquidity": 2e4,
                                          "url": "https://d/%d" % i}, 2.6, time.time() + i,
                             {"level": "LOW"})
        out = self.cmd("/last")
        self.assertIn("Last 5 alerts", out)
        self.assertIn("C6", out)
        self.assertNotIn("C1)", out)
        self.assertLess(out.index("C6"), out.index("C2"))  # newest first
        self.assertIn("risk LOW", out)

    def test_pause_resume_persisted(self):
        self.assertIn("paused", self.cmd("/pause"))
        self.assertTrue(sva.load_state(sva.STATE_FILE)["paused"])
        self.assertIn("already paused", self.cmd("/pause"))
        self.assertIn("PAUSED", self.cmd("/status"))
        self.assertIn("resumed", self.cmd("/resume"))
        self.assertFalse(sva.load_state(sva.STATE_FILE)["paused"])
        self.assertIn("already active", self.cmd("/resume"))

    def test_command_with_botname_and_unknown(self):
        self.assertIn("Commands", self.cmd("/help@my_shadow_bot"))
        self.assertIn("Unknown command", self.cmd("/foo"))
        self.assertIsNone(self.cmd("hello"))

    def test_poll_replies_and_persists_offset(self):
        self.session.respond("getUpdates", 200, {"ok": True, "result": [update(41, "/status"), update(42, "/help")]})
        wait = sva.poll_once(self.tg, self.cfg, self.state, time.time())
        self.assertEqual(wait, 0)
        sent = self.session.sent()
        self.assertEqual(len(sent), 2)
        self.assertEqual(str(sent[0]["chat_id"]), CHAT_ID)
        self.assertEqual(sent[0]["parse_mode"], "HTML")
        self.assertEqual(sent[0]["reply_to_message_id"], 410)
        self.assertEqual(self.state["tg_offset"], 43)
        self.assertEqual(sva.load_state(sva.STATE_FILE)["tg_offset"], 43)
        # next poll must send the stored offset
        sva.poll_once(self.tg, self.cfg, self.state, time.time())
        polls = [p for m, p in self.session.calls if m == "getUpdates"]
        self.assertEqual(polls[-1]["offset"], 43)

    def test_ignores_other_chats_and_old_commands(self):
        self.session.respond("getUpdates", 200, {"ok": True, "result": [
            update(1, "/pause", chat_id="999"),
            update(2, "/pause", date=time.time() - 3600),
            update(3, "just text")]})
        sva.poll_once(self.tg, self.cfg, self.state, time.time())
        self.assertEqual(self.session.sent(), [])
        self.assertFalse(self.state["paused"])
        self.assertEqual(self.state["tg_offset"], 4)  # still acknowledged so they are not re-delivered

    def test_channel_post_by_username(self):
        self.cfg["chat_id"] = "@mychannel"
        upd = {"update_id": 5, "channel_post": {"message_id": 1, "date": int(time.time()), "text": "/status",
                                                "chat": {"id": -1001, "type": "channel", "username": "MyChannel"}}}
        self.session.respond("getUpdates", 200, {"ok": True, "result": [upd]})
        sva.poll_once(self.tg, self.cfg, self.state, time.time())
        self.assertEqual(len(self.session.sent()), 1)

    def test_error_handling(self):
        self.session.respond("getUpdates", 429, {"ok": False, "parameters": {"retry_after": 7}})
        self.assertEqual(sva.poll_once(self.tg, self.cfg, self.state, time.time()), 8)
        self.session.respond("getUpdates", 409, {"ok": False, "description": "Conflict: webhook is active"})
        self.assertEqual(sva.poll_once(self.tg, self.cfg, self.state, time.time()), 30)
        self.assertEqual(self.session.calls[-1][0], "deleteWebhook")
        self.session.respond("getUpdates", 502, {"ok": False, "description": "Bad Gateway"})
        self.assertEqual(sva.poll_once(self.tg, self.cfg, self.state, time.time()), 10)

        class Broken(FakeSession):
            def post(self, url, json=None, timeout=None):
                raise sva.requests.ConnectionError("offline")
        tg = sva.Telegram("TEST", CHAT_ID, False, session=Broken())
        self.assertEqual(sva.poll_once(tg, self.cfg, self.state, time.time()), 10)

    def test_send_flood_retry(self):
        orig = sva.time.sleep
        sva.time.sleep = lambda s: None
        try:
            self.session.respond("sendMessage", 429, {"ok": False, "parameters": {"retry_after": 3}})
            self.assertTrue(self.tg.send("hi"))
            self.assertEqual(len(self.session.sent()), 2)
            self.session.respond("sendMessage", 400, {"ok": False, "description": "chat not found"})
            self.assertFalse(self.tg.send("hi"))
        finally:
            sva.time.sleep = orig

    def test_setup_commands(self):
        self.assertTrue(self.tg.setup_commands())
        methods = [m for m, _ in self.session.calls]
        self.assertEqual(methods[:2], ["deleteWebhook", "setMyCommands"])
        cmds = self.session.calls[1][1]["commands"]
        self.assertEqual([c["command"] for c in cmds], [c for c, _ in sva.BOT_COMMANDS])


class SafetyTests(unittest.TestCase):
    class FakeHttp:
        def __init__(self, data):
            self.data = data

        def get_json(self, url, **kw):
            return self.data

    def test_rugcheck_parsing(self):
        data = {"risks": [{"name": "Single holder ownership", "value": "43%", "score": 8727, "level": "danger"},
                          {"name": "Single holder ownership", "value": "43%", "score": 4363, "level": "danger"},
                          {"name": "Low Liquidity", "value": "$0", "score": 2999, "level": "danger"}],
                "score_normalised": 55, "mintAuthority": None, "freezeAuthority": "Frz111", "rugged": False,
                "totalHolders": 900, "markets": [{"pubkey": "PAIR", "lp": {"lpLockedPct": 100}}]}
        s = sva.rugcheck_report(self.FakeHttp(data), "MINT", "PAIR")
        self.assertEqual(s["level"], "HIGH")
        self.assertEqual(len(s["risks"]), 2)  # duplicates removed
        self.assertEqual(s["lp_locked"], 100)
        text = "\n".join(sva.safety_lines(s, "MINT"))
        self.assertIn("Freeze authority: ⚠️ ENABLED", text)
        self.assertIn("Mint authority: ✅ revoked", text)
        self.assertTrue(sva.is_high_risk(s))

    def test_rugcheck_clean_and_unavailable(self):
        s = sva.rugcheck_report(self.FakeHttp({"risks": [], "score_normalised": 1}), "MINT")
        self.assertEqual(s["level"], "LOW")
        self.assertFalse(sva.is_high_risk(s))
        self.assertIsNone(sva.rugcheck_report(self.FakeHttp(None), "MINT"))
        self.assertIn("unavailable", sva.safety_lines(None, "MINT")[0])


if __name__ == "__main__":
    unittest.main()
