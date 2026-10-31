import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import account  # noqa: E402
import lighter_api as api  # noqa: E402

ME = 44
WALLET = "0x" + "1" * 40


def ms(y, mo, d, h, mi=0):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=account.TR).timestamp() * 1000)


def rest_trade(tid, ts, i_sell=False, i_maker=False, usd=100.0, order=None, sym="AAPL"):
    """Сделка из /trades: моя сторона и роль задаются явно."""
    me_ask = i_sell
    maker_ask = me_ask if i_maker else not me_ask
    return {"trade_id": tid, "tx_hash": f"h{tid}", "type": "trade", "market_symbol": sym, "timestamp": ts,
            "size": "1", "price": str(usd), "usd_amount": str(usd), "is_maker_ask": maker_ask,
            "ask_account_id": ME if me_ask else 900 + tid, "bid_account_id": 900 + tid if me_ask else ME,
            "ask_id": order or 5000 + tid, "bid_id": order or 6000 + tid}


class Week(unittest.TestCase):
    def test_week_boundary_is_friday_15_trt(self):
        s, e = account.week_of(ms(2026, 10, 30, 15, 0))
        self.assertEqual(s, dt.datetime(2026, 10, 30, 15, 0, tzinfo=account.TR))
        self.assertEqual(e - s, dt.timedelta(days=7))
        s2, _ = account.week_of(ms(2026, 10, 30, 14, 59))
        self.assertEqual(s2, dt.datetime(2026, 10, 23, 15, 0, tzinfo=account.TR))
        s3, _ = account.week_of(ms(2026, 9, 1, 12))   # и до опорной даты
        self.assertEqual(s3.weekday(), 4)
        self.assertEqual((s3.hour, s3.minute), (15, 0))


class Classify(unittest.TestCase):
    def test_roles_counterparty_tiers_and_duplicates(self):
        t1 = rest_trade(1, 1000, i_sell=False, i_maker=False)   # я тейкер, покупка: комиссия контрагента = mf
        t2 = rest_trade(2, 2000, i_sell=True, i_maker=True)     # я мейкер, продажа: комиссия контрагента = tf
        t3 = rest_trade(3, 3000)
        fees = {1: {"tf": 0, "mf": 120}, 2: {"tf": 50, "mf": 0}}
        rows = account.classify([t1, t2, t3, t1], fees, ME)
        self.assertEqual([r["trade_id"] for r in rows], [1, 2, 3])
        self.assertEqual((rows[0]["side"], rows[0]["my_role"], rows[0]["cp_tier"]), ("buy", "taker", "premium"))
        self.assertEqual(rows[0]["counterparty"], 901)
        self.assertEqual((rows[1]["side"], rows[1]["my_role"], rows[1]["cp_tier"]), ("sell", "maker", "plus"))
        self.assertEqual(rows[1]["counterparty"], 902)
        self.assertEqual(rows[1]["my_order"], 5002)
        self.assertEqual(rows[2]["cp_tier"], "unknown")

    def test_agg(self):
        rows = [{"usd": 100, "cp_tier": "premium"}, {"usd": 300, "cp_tier": "standard"}, {"usd": 0, "cp_tier": "unknown"}]
        a = account.agg(rows)
        self.assertEqual((a["trades"], a["usd"], a["premium_usd"]), (3, 400, 100))
        self.assertAlmostEqual(a["premium_pct_usd"], 25)
        self.assertAlmostEqual(a["premium_pct_n"], 100 / 3)
        self.assertEqual(account.agg([])["premium_pct_usd"], 0)


class Resolve(unittest.TestCase):
    def test_bad_address(self):
        for bad in ("", "0x123", "1" * 42, "0x" + "g" * 40, None):
            with self.assertRaises(account.WalletError):
                account.resolve(bad)

    def test_main_account_is_picked(self):
        d = {"accounts": [{"index": 90, "account_type": 1}, {"index": 50, "account_type": 0}, {"index": 70, "account_type": 0}]}
        with mock.patch("lighter_api.get", return_value=d) as g:
            info = account.resolve(WALLET)
        self.assertIn(WALLET, g.call_args[0][1])
        self.assertEqual(info, {"wallet": WALLET, "account": 50, "sub_accounts": [50, 70, 90]})

    def test_no_account(self):
        with mock.patch("lighter_api.get", side_effect=api.ApiError("HTTP 400")):
            with self.assertRaisesRegex(account.WalletError, "нет аккаунта"):
                account.resolve(WALLET)
        with mock.patch("lighter_api.get", return_value={"accounts": []}):
            with self.assertRaisesRegex(account.WalletError, "нет аккаунта"):
                account.resolve(WALLET)
        with mock.patch("lighter_api.get", side_effect=api.ApiError("не удалось: timeout")):
            with self.assertRaisesRegex(account.WalletError, "не ответил"):
                account.resolve(WALLET)


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = account.Tracker(self.tmp.name)
        self.changed = []
        self.t.on_change.append(lambda: self.changed.append(1))
        with mock.patch("lighter_api.get", return_value={"accounts": [{"index": ME, "account_type": 0}]}):
            self.t.set_wallet("  " + WALLET + " ")

    def tearDown(self):
        self.tmp.cleanup()

    def test_wallet_saved_and_reloaded(self):
        self.assertEqual(self.t.info(), {"wallet": WALLET, "account": ME, "sub_accounts": [ME]})
        self.assertEqual(self.changed, [1])
        self.assertTrue(self.t.wake.is_set())
        cfg = json.load(open(os.path.join(self.tmp.name, "config.json")))
        self.assertEqual(cfg["account"], ME)
        self.assertEqual(account.Tracker(self.tmp.name).account(), ME)
        with self.assertRaises(account.WalletError):   # неверный адрес не трогает текущий
            self.t.set_wallet("0xnope")
        self.assertEqual(self.t.wallet(), WALLET)

    def test_live_fees_only_for_my_trades(self):
        now = int(time.time() * 1000)
        mine = {**rest_trade(1, now), "maker_fee": 120}
        other = {**rest_trade(2, now), "ask_account_id": 1, "bid_account_id": 2, "maker_fee": 120}
        self.t.have_fee = set()
        self.t.observe_live([mine, other])
        self.t.observe_live([mine])   # повтор не дублирует
        _, fees = self.t.paths(ME)
        recs = account.read_jsonl(fees)
        self.assertEqual(recs, [{"trade_id": 1, "tf": 0, "mf": 120, "tx_type": "trade", "src": "ws"}])

    def test_step_downloads_trades_and_missing_fees(self):
        now = int(time.time() * 1000)
        week_start = int(account.week_of(now)[0].timestamp() * 1000)
        trades = [rest_trade(3, now - 1000, usd=300), rest_trade(2, now - 2000, usd=200),
                  rest_trade(1, week_start - 5000, usd=100)]   # прошлая неделя
        # для сделки 3 комиссия уже пришла из живой ленты
        self.t.observe_live([{**trades[0], "maker_fee": 120}])
        calls = []

        def fake_get(venue, path, low=False, **kw):
            calls.append((path.split("?")[0], low))
            if path.startswith("trades?"):
                return {"trades": trades, "next_cursor": None}
            tid = int(path.split("value=h")[1])
            return {"type": 14, "event_info": json.dumps({"t": {"tf": 0, "mf": 0 if tid == 2 else 84}})}

        self.t.wake.clear()
        with mock.patch("lighter_api.get", side_effect=fake_get), mock.patch.object(account, "TX_EVERY", 0):
            self.assertGreater(self.t.step(), time.time())
        self.assertEqual(calls[0], ("trades", False))
        self.assertEqual(sorted(c for c in calls if c[0] == "tx"), [("tx", True), ("tx", True)])  # 2 и 1, без 3
        p = self.t.payload()
        self.assertEqual(p["account"], ME)
        self.assertEqual(p["week"]["trades"], 2)
        self.assertAlmostEqual(p["week"]["usd"], 500)
        self.assertAlmostEqual(p["week"]["premium_usd"], 300)
        self.assertEqual([r["trade_id"] for r in p["trades"]], [3, 2, 1])
        self.assertEqual(p["by_symbol"][0]["symbol"], "AAPL")
        self.assertEqual(p["pending"], 0)
        # следующая проверка: новых сделок нет — повторных запросов /tx тоже
        calls.clear()
        with mock.patch("lighter_api.get", side_effect=fake_get):
            self.t.step()
        self.assertEqual([c[0] for c in calls], ["trades"])

    def test_fetch_stops_at_known_trade(self):
        pages = [{"trades": [rest_trade(5, 5), rest_trade(4, 4)], "next_cursor": "c1"},
                 {"trades": [rest_trade(3, 3), rest_trade(2, 2)], "next_cursor": "c2"}]
        with mock.patch("lighter_api.get", side_effect=pages) as g:
            new = self.t.fetch_new_trades(ME, {3, 2, 1}, 0)
        self.assertEqual([t["trade_id"] for t in new], [5, 4])
        self.assertEqual(g.call_count, 2)
        self.assertIn("cursor=c1", g.call_args[0][1])

    def test_payload_without_wallet(self):
        p = account.Tracker(tempfile.mkdtemp(dir=self.tmp.name)).payload()
        self.assertIsNone(p["account"])
        self.assertEqual(p["trades"], [])
        self.assertEqual(p["week"]["usd"], 0)


if __name__ == "__main__":
    unittest.main()
