import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lighter_api as api  # noqa: E402
import market  # noqa: E402


def rh_market(mid, sym, mult=50, status="active"):
    return {"market_id": mid, "symbol": sym, "market_type": "perp", "status": status,
            "funding_premium_multiplier": mult, "supported_price_decimals": 2, "supported_size_decimals": 4}


def trade(tid, maker_ask, ask, bid, maker_fee=None, taker_fee=None, ts=1_700_000_000_000, kind="trade"):
    t = {"trade_id": tid, "type": kind, "timestamp": ts, "is_maker_ask": maker_ask,
         "ask_account_id": ask, "bid_account_id": bid}
    if maker_fee is not None:
        t["maker_fee"] = maker_fee
    if taker_fee is not None:
        t["taker_fee"] = taker_fee
    return t


class TierAndBook(unittest.TestCase):
    def test_tier_of_fee(self):
        self.assertEqual(market.tier_of_fee(None), "standard")
        self.assertEqual(market.tier_of_fee(0), "standard")
        self.assertEqual(market.tier_of_fee(50), "plus")
        self.assertEqual(market.tier_of_fee(120), "premium")

    def test_book_snapshot_delta_and_gap(self):
        b = market.Book()
        self.assertTrue(b.apply({"bids": [{"price": "10", "size": "2"}], "asks": [{"price": "11", "size": "1"}], "nonce": 5}, True))
        self.assertTrue(b.apply({"begin_nonce": 5, "nonce": 6, "asks": [{"price": "11", "size": "0"}, {"price": "12", "size": "3"}]}, False))
        self.assertEqual(b.levels("buy"), [(12.0, 3.0)])
        self.assertEqual(b.levels("sell"), [(10.0, 2.0)])
        # пропуск номера обновления: стакан недействителен, пока не придёт новый снимок
        self.assertFalse(b.apply({"begin_nonce": 9, "nonce": 10, "bids": []}, False))
        self.assertFalse(b.valid)
        self.assertFalse(b.apply({"begin_nonce": 6, "nonce": 7}, False))

    def test_fill(self):
        lv = [(10.0, 1.0), (11.0, 2.0)]
        self.assertEqual(market.fill(lv, 0), 10.0)
        self.assertAlmostEqual(market.fill(lv, 2), 10.5)
        self.assertAlmostEqual(market.fill(lv, 3), 32 / 3)
        self.assertIsNone(market.fill(lv, 3.5))
        self.assertIsNone(market.fill([], 1))


class Markets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = market.Market(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_rwa_and_pre_ipo_present_on_both_venues(self):
        rh = [rh_market(1, "BTC", 100), rh_market(2, "AAPL"), rh_market(3, "SPACEX", 1), rh_market(4, "LONELY"),
              rh_market(5, "OLD", status="inactive"), rh_market(6, "SOL", 50)]
        core = [{**rh_market(11, "AAPL"), "funding_premium_multiplier": 50}, {**rh_market(12, "SPACEX"), "funding_premium_multiplier": 1},
                {**rh_market(13, "BTC"), "funding_premium_multiplier": 100},
                {**rh_market(14, "SOL"), "funding_premium_multiplier": 100}]   # одноимённая крипта на Core — не пара
        self.m.set_markets(rh, core)
        got = self.m.markets_payload()
        self.assertEqual([x["symbol"] for x in got], ["AAPL", "SPACEX"])
        self.assertEqual({x["symbol"]: x["kind"] for x in got}, {"AAPL": "RWA", "SPACEX": "pre-IPO"})
        self.assertEqual(self.m.pairs["AAPL"], {"rh": 2, "core": 11})
        self.assertIn("LONELY", self.m.rwa)            # есть на RH, но без пары на Core — в список не идёт
        self.assertEqual(len(self.m.rh_markets), 6)   # лента сделок (реестр тарифов) — по всем перпам RH, и крипте тоже

    def test_registry_from_trades(self):
        self.m.observe([trade(1, True, ask=100, bid=200, maker_fee=120, ts=2000),       # 100 — мейкер premium, 200 — тейкер standard
                        trade(2, False, ask=300, bid=400, maker_fee=50, taker_fee=84, ts=2000),
                        trade(3, True, ask=500, bid=600, maker_fee=120, ts=2000, kind="liquidation")])
        reg = self.m.reg_copy()
        self.assertEqual(reg["100"], ["premium", 2])
        self.assertEqual(reg["200"], ["standard", 2])
        self.assertEqual(reg["400"], ["plus", 2])
        self.assertEqual(reg["300"], ["premium", 2])
        self.assertNotIn("500", reg)  # ликвидации тариф не определяют
        self.m.observe([trade(4, True, ask=100, bid=1, ts=1000)])  # более старая сделка не перетирает
        self.assertEqual(self.m.reg_copy()["100"][0], "premium")
        self.m.save_reg()
        self.assertEqual(json.load(open(os.path.join(self.tmp.name, "tier_registry.json")))["100"][0], "premium")
        self.assertEqual(market.Market(self.tmp.name).reg_copy()["100"][0], "premium")

    def test_listener_error_does_not_break_feed(self):
        seen = []
        self.m.listeners += [lambda t: 1 / 0, seen.append]
        q = self.m.subscribe()
        self.m.on_trades([trade(1, True, 1, 2)])
        self.assertEqual(len(seen), 1)
        self.assertIn("ZeroDivisionError", self.m.error)
        self.assertEqual(json.loads(q.get_nowait())["trades"][0]["trade_id"], 1)
        self.m.unsubscribe(q)
        self.m.on_trades([trade(2, True, 1, 2)])
        self.assertTrue(q.empty())


ACCOUNT_RH = {"accounts": [{"index": 7, "collateral": "1000", "total_asset_value": "1100", "available_balance": "600",
                            "cross_initial_margin_requirement": "500", "cross_maintenance_margin_requirement": "250",
                            "positions": [
                                {"symbol": "AAPL", "position": "10", "sign": 1, "avg_entry_price": "100",
                                 "unrealized_pnl": "5", "position_value": "1005", "total_funding_paid_out": "-1.5",
                                 "liquidation_price": "60"},
                                {"symbol": "ZERO", "position": "0", "sign": 1, "avg_entry_price": "1"}]}]}
ACCOUNT_CORE = {"accounts": [{"index": 9, "total_asset_value": "900", "available_balance": "400",
                              "positions": [{"symbol": "AAPL", "position": "10", "sign": -1, "avg_entry_price": "101",
                                             "unrealized_pnl": "-3", "position_value": "1005",
                                             "total_funding_paid_out": "2.5", "liquidation_price": "140"}]}]}


def book(bids, asks):
    b = market.Book()
    b.apply({"bids": [{"price": str(p), "size": str(z)} for p, z in bids],
             "asks": [{"price": str(p), "size": str(z)} for p, z in asks], "nonce": 1}, True)
    return b


class Positions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wallet = "0x" + "a" * 40
        self.m = market.Market(self.tmp.name, wallet=lambda: self.wallet)
        self.m.set_markets([rh_market(2, "AAPL")], [{**rh_market(11, "AAPL")}])

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_accounts(self):
        pos, accs = market.parse_accounts(ACCOUNT_RH)
        self.assertEqual(list(pos), ["AAPL"])          # нулевая позиция пропущена
        self.assertEqual(pos["AAPL"]["size"], 10)
        self.assertAlmostEqual(pos["AAPL"]["mark"], 100.5)
        self.assertEqual(accs[0]["index"], 7)
        self.assertEqual(market.parse_accounts(ACCOUNT_CORE)[0]["AAPL"]["size"], -10)
        self.assertEqual(market.parse_accounts({}), ({}, []))

    def fake_get(self, venue, path, **kw):
        self.assertIn(self.wallet, path)
        if venue == "core" and getattr(self, "core_missing", False):
            raise api.ApiError("HTTP 400")
        return ACCOUNT_RH if venue == "rh" else ACCOUNT_CORE

    def test_load_positions_and_hedge(self):
        with mock.patch("lighter_api.get", side_effect=self.fake_get):
            self.m.load_positions()
        self.assertEqual(self.m.pos_wallet, self.wallet)
        self.assertEqual(self.m.hedge_dir("AAPL"), "rh_long")
        self.assertEqual(self.m.wanted_books("rh"), {2})
        self.assertEqual(self.m.wanted_books("core"), {11})
        # без стаканов: вход считается, выход — нет
        h = self.m.hedges()[0]
        self.assertAlmostEqual(h["entry_pct"], 1.0)    # (101 − 100) / 100
        self.assertNotIn("exit_pct", h)
        self.assertAlmostEqual(h["upnl"], 2.0)
        self.assertAlmostEqual(h["funding_net"], 1.0)
        # стаканы: RH bid 100.4 / ask 100.6, Core bid 100.5 / ask 100.7
        self.m.books["rh"][2] = book([(100.4, 50)], [(100.6, 50)])
        self.m.books["core"][11] = book([(100.5, 50)], [(100.7, 50)])
        self.m.mstats["rh"][2] = {"current_funding_rate": "0.001"}
        self.m.mstats["core"][11] = {"current_funding_rate": "0.004"}
        h = self.m.hedges()[0]
        # закрыть LONG RH / SHORT Core: продать RH по 100.4, купить Core по 100.7
        self.assertAlmostEqual(h["exit_pct"], 100 * 0.3 / 100.5)
        self.assertAlmostEqual(h["profit_pct"], 1.0 - 100 * 0.3 / 100.5)
        self.assertAlmostEqual(h["close_pnl"], 10 * 0.4 + 10 * 0.3)
        self.assertAlmostEqual(h["notional"], 1005)
        self.assertAlmostEqual(h["carry_next_pct_h"], 0.003)  # шорт на Core получает 0.004, лонг на RH платит 0.001

    def test_core_long_direction(self):
        self.m.set_positions(self.wallet, {
            "rh": ({"AAPL": {"size": -5, "entry": 102, "upnl": 0, "funding_paid": 0}}, []),
            "core": ({"AAPL": {"size": 5, "entry": 100, "upnl": 0, "funding_paid": 0}}, [])})
        self.m.books["rh"][2] = book([(100.4, 50)], [(100.6, 50)])
        self.m.books["core"][11] = book([(100.5, 50)], [(100.7, 50)])
        h = self.m.hedges()[0]
        self.assertEqual(h["dir"], "core_long")
        self.assertAlmostEqual(h["entry_pct"], 100 * 2 / 102)
        # закрыть: купить RH по 100.6, продать Core по 100.5
        self.assertAlmostEqual(h["exit_pct"], 100 * 0.1 / 100.5)
        self.assertAlmostEqual(h["close_pnl"], 5 * (100.5 - 100) + 5 * (102 - 100.6))

    def test_single_leg_and_unpaired(self):
        self.m.set_positions(self.wallet, {"rh": ({"TSLA": {"size": 1, "entry": 1, "upnl": 1, "funding_paid": 0}}, []), "core": ({}, [])})
        h = self.m.hedges()[0]
        self.assertIsNone(h["dir"])
        self.assertFalse(h["paired"])
        self.assertEqual(self.m.wanted_books("rh"), set())

    def test_missing_core_account_and_wallet_change(self):
        self.core_missing = True
        with mock.patch("lighter_api.get", side_effect=self.fake_get):
            self.m.load_positions()
        self.assertEqual(self.m.accounts["core"], [])
        self.assertEqual(list(self.m.positions["rh"]), ["AAPL"])
        # кошелёк сменили, пока шли запросы — старые позиции не записываются
        def switching(venue, path, **kw):
            self.wallet = "0x" + "b" * 40
            return ACCOUNT_RH
        self.m.wallet_changed()
        with mock.patch("lighter_api.get", side_effect=switching):
            self.m.load_positions()
        self.assertEqual(self.m.positions["rh"], {})
        self.assertIsNone(self.m.pos_wallet)

    def test_no_wallet(self):
        self.wallet = None
        with mock.patch("lighter_api.get") as g:
            self.m.load_positions()
        g.assert_not_called()
        self.assertEqual(self.m.hedges(), [])
        self.assertIsNone(self.m.status()["positions_age"])


if __name__ == "__main__":
    unittest.main()
