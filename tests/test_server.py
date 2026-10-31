import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lighter_api as api  # noqa: E402
import server  # noqa: E402

WALLET = "0x" + "2" * 40


class ServerTest(unittest.TestCase):
    """Настоящий HTTP-сервер на свободном порту; фоновые потоки к Lighter не запускаются."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = server.App(self.tmp.name)
        self.app.market.set_markets(
            [{"market_id": 2, "symbol": "AAPL", "market_type": "perp", "status": "active", "funding_premium_multiplier": 50,
              "supported_price_decimals": 2, "supported_size_decimals": 4}],
            [{"market_id": 11, "symbol": "AAPL", "market_type": "perp", "status": "active", "funding_premium_multiplier": 50,
              "supported_price_decimals": 2, "supported_size_decimals": 4}])
        self.srv = server.Server(("127.0.0.1", 0), server.make_handler(self.app))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=body if isinstance(body, bytes) else json.dumps(body).encode(),
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, json.loads(body) if "json" in (e.headers.get("Content-Type") or "") else body

    def test_static_files(self):
        code, ctype, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b'id="wallet"', body)
        self.assertIn(b'src="/book.js"', body)
        code, ctype, body = self.get("/book.js")
        self.assertEqual(code, 200)
        self.assertIn("javascript", ctype)
        self.assertEqual(self.get("/../server.py")[0], 404)
        self.assertEqual(self.get("/nope")[0], 404)

    def test_markets_and_registry(self):
        code, _, body = self.get("/markets")
        self.assertEqual([m["symbol"] for m in json.loads(body)], ["AAPL"])
        self.app.market.reg["5"] = ["premium", 1]
        self.assertEqual(json.loads(self.get("/registry")[2]), {"5": ["premium", 1]})

    def test_owners_cached_and_validated(self):
        self.assertEqual(self.get("/owners")[0], 400)
        self.assertEqual(self.get("/owners?market_id=x")[0], 400)
        with mock.patch("lighter_api.get", return_value={"asks": [], "bids": []}) as g:
            for _ in range(3):
                code, _, body = self.get("/owners?market_id=2")
                self.assertEqual(code, 200)
        self.assertEqual(g.call_count, 1)   # один запрос к Lighter на все вкладки
        self.assertIn("orderBookOrders?market_id=2", g.call_args[0][1])
        with mock.patch("lighter_api.get", side_effect=api.ApiError("очередь запросов занята")):
            code, _, body = self.get("/recent?market_id=3")
        self.assertEqual(code, 503)
        self.assertIn("очередь", json.loads(body)["error"])

    def test_wallet_flow(self):
        self.assertEqual(json.loads(self.get("/wallet")[2])["wallet"], None)
        my = json.loads(self.get("/my")[2])
        self.assertIsNone(my["account"])
        self.assertEqual(json.loads(self.get("/hedges")[2])["hedges"], [])
        self.assertEqual(self.post("/wallet", b"{bad")[0], 400)
        code, d = self.post("/wallet", {"wallet": "0x12"})
        self.assertEqual(code, 400)
        self.assertIn("40", d["error"])
        with mock.patch("lighter_api.get", return_value={"accounts": [{"index": 77, "account_type": 0}]}):
            code, d = self.post("/wallet", {"wallet": WALLET})
        self.assertEqual((code, d["account"]), (200, 77))
        self.assertEqual(json.loads(self.get("/wallet")[2])["account"], 77)
        st = json.loads(self.get("/status")[2])
        self.assertEqual(st["wallet"]["wallet"], WALLET)
        # позиции нового кошелька ещё не загружены — хеджей и счетов прошлого не показываем
        self.app.market.set_positions("0x" + "3" * 40, {"rh": ({"AAPL": {"size": 1, "entry": 1, "upnl": 0, "funding_paid": 0}}, [{"index": 1}])})
        h = json.loads(self.get("/hedges")[2])
        self.assertEqual((h["hedges"], h["accounts"]), ([], {}))
        self.app.market.set_positions(WALLET, {"rh": ({"AAPL": {"size": 1, "entry": 1, "upnl": 0, "funding_paid": 0}}, [{"index": 77}])})
        h = json.loads(self.get("/hedges")[2])
        self.assertEqual(h["hedges"][0]["symbol"], "AAPL")
        self.assertEqual(h["accounts"]["rh"][0]["index"], 77)
        self.assertEqual(self.post("/nope", {})[0], 404)

    def test_events_stream(self):
        r = urllib.request.urlopen(self.base + "/events", timeout=5)
        self.assertEqual(r.readline(), b"retry: 2000\n")
        r.readline()
        for _ in range(50):   # подписка появляется после ответа сервера
            if self.app.market.subs:
                break
            threading.Event().wait(0.02)
        self.app.market.on_trades([{"trade_id": 1, "type": "trade", "timestamp": 1000, "is_maker_ask": True,
                                    "ask_account_id": 1, "bid_account_id": 2}])
        line = r.readline()
        self.assertTrue(line.startswith(b"data: "))
        self.assertEqual(json.loads(line[6:])["trades"][0]["trade_id"], 1)
        r.close()


class MainTest(unittest.TestCase):
    """Повторный запуск (двойной щелчок по Start.bat) и занятый порт."""

    def test_second_launch_opens_running_page(self):
        tmp = tempfile.TemporaryDirectory()
        app = server.App(tmp.name)
        srv = server.Server(("127.0.0.1", 0), server.make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        try:
            with mock.patch("webbrowser.open") as wb, mock.patch.object(server, "App") as new_app:
                self.assertEqual(server.main(["--port", str(port)]), 0)
            wb.assert_called_once_with(f"http://127.0.0.1:{port}")
            new_app.assert_not_called()   # второй сервер не создаётся
        finally:
            srv.shutdown()
            srv.server_close()
            tmp.cleanup()

    def test_port_busy_with_other_program(self):
        import socket
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        try:
            with mock.patch.object(server, "already_running", return_value=False), \
                    mock.patch.object(server, "App") as new_app, mock.patch("sys.stderr"):
                self.assertEqual(server.main(["--port", str(port), "--no-browser"]), 2)
            new_app.assert_not_called()
        finally:
            busy.close()


if __name__ == "__main__":
    unittest.main()
