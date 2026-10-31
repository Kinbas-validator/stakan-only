"""Рынки Lighter, живая лента сделок и позиции по кошельку.

Работает в фоне внутри server.py:
  * тикеры — перпы RH с funding_premium_multiplier != 100 (у крипты 100, у акций/ETF/металлов 50,
    у pre-IPO 1), у которых есть такой же тикер на Lighter Core;
  * WebSocket trade/* по всем рынкам RH — реестр тарифов аккаунтов (premium / plus / standard)
    по комиссиям в сделках и живая лента для страницы;
  * WebSocket order_book обеих площадок — только по тикерам с открытой позицией (для «Моих хеджей»)
    + market_stats/all (марк-цены и ставки фандинга всех рынков одной подпиской);
  * позиции и счета обеих площадок по адресу кошелька — хеджи без ручного ввода.

Котировки USDG (RH) и USDC (Core) считаются равными $1.
Спред хеджа — в % от цены входа RH, «канонически» для LONG RH / SHORT Core:
  вход  = (Core − RH) по средним ценам позиций; выход сейчас = (Core ask − RH bid) по стакану.
Для LONG Core / SHORT RH знаки обратные. Заработок по спреду = вход − выход.
"""
import asyncio
import json
import os
import queue
import threading
import time
from collections import defaultdict

import websockets

import lighter_api as api

POSITIONS_EVERY = 15
CRYPTO_MULT = 100
DIRS = {"rh_long": "LONG RH / SHORT Core", "core_long": "LONG Core / SHORT RH"}


def tier_of_fee(fee):
    """Тариф по комиссии стороны сделки (единицы 1e-6 от объёма): 0 — standard, 50 — plus, больше — premium."""
    if not fee:
        return "standard"
    return "plus" if fee == 50 else "premium"


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class Book:
    """Стакан из WS: снимок + изменения; верен, пока номера обновлений идут без пропусков."""
    def __init__(self):
        self.bids, self.asks = {}, {}
        self.nonce, self.valid, self.ts = None, False, 0.0

    def apply(self, ob, snapshot):
        if snapshot:
            self.bids, self.asks = {}, {}
        elif not self.valid or ob.get("begin_nonce") != self.nonce:
            self.valid = False
            return False
        for side, d in (("bids", self.bids), ("asks", self.asks)):
            for lvl in ob.get(side) or []:
                p, z = float(lvl["price"]), float(lvl["size"])
                if z <= 0:
                    d.pop(p, None)
                else:
                    d[p] = z
        self.nonce, self.valid, self.ts = ob.get("nonce"), True, time.time()
        return True

    def levels(self, side):
        return sorted(self.asks.items()) if side == "buy" else sorted(self.bids.items(), reverse=True)


def fill(levels, qty):
    """Средняя цена исполнения qty акций по уровням; None — не хватает глубины."""
    if not levels:
        return None
    if qty <= 0:
        return levels[0][0]
    need, cost = qty, 0.0
    for p, z in levels:
        take = min(z, need)
        cost += take * p
        need -= take
        if need <= 1e-12:
            return cost / qty
    return None


class Market:
    def __init__(self, data_dir, wallet=lambda: None):
        self.wallet = wallet                          # функция: текущий адрес кошелька или None
        self.reg_path = os.path.join(data_dir, "tier_registry.json")
        self.lock = threading.RLock()
        self.rh_markets, self.core_markets = {}, {}   # market_id -> детали
        self.rwa = {}                                  # symbol -> детали RH + core_market_id
        self.pairs = {}                                # symbol -> {"rh": mid, "core": mid}
        self.books = {"rh": defaultdict(Book), "core": defaultdict(Book)}
        self.mstats = {"rh": {}, "core": {}}
        self.conn = {k: {"ok": False, "last": 0.0, "err": "", "reconnects": 0} for k in ("rh", "core", "trades")}
        self.restart = {"trades": False}               # новые рынки RH — переподписать ленту сделок
        self.positions = {"rh": {}, "core": {}}
        self.accounts = {}
        self.pos_ts, self.pos_err, self.pos_wallet = 0.0, "", None
        self.pos_wake = threading.Event()
        self.subs, self.listeners = [], []
        self.reg = self._load_reg()
        self.reg_dirty = False
        self.error = ""

    # ---------- реестр тарифов ----------
    def _load_reg(self):
        try:
            with open(self.reg_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_reg(self):
        with self.lock:
            if not self.reg_dirty:
                return
            tmp = self.reg_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.reg, f)
            os.replace(tmp, self.reg_path)
            self.reg_dirty = False

    def reg_copy(self):
        with self.lock:
            return dict(self.reg)

    def observe(self, trades):
        """Тариф аккаунта — по его комиссии в последней обычной сделке (в ликвидациях платит и standard)."""
        with self.lock:
            for t in trades:
                if t.get("type") != "trade":
                    continue
                ts = t["timestamp"] // 1000
                maker = t["ask_account_id"] if t["is_maker_ask"] else t["bid_account_id"]
                taker = t["bid_account_id"] if t["is_maker_ask"] else t["ask_account_id"]
                for acc, fee in ((maker, t.get("maker_fee")), (taker, t.get("taker_fee"))):
                    k = str(acc)
                    if k not in self.reg or self.reg[k][1] <= ts:
                        self.reg[k] = [tier_of_fee(fee), ts]
                        self.reg_dirty = True

    # ---------- рынки ----------
    def load_markets(self):
        rh = api.get("rh", "orderBookDetails")["order_book_details"]
        core = api.get("core", "orderBookDetails")["order_book_details"]
        self.set_markets(rh, core)

    def set_markets(self, rh, core):
        rh_perp = {m["market_id"]: m for m in rh if m["market_type"] == "perp"}
        core_by_sym = {m["symbol"]: m for m in core if m["market_type"] == "perp" and m["status"] == "active"}
        rwa, pairs = {}, {}
        for m in rh_perp.values():
            if m["status"] != "active" or int(m.get("funding_premium_multiplier", CRYPTO_MULT)) == CRYPTO_MULT:
                continue
            c = core_by_sym.get(m["symbol"])
            if c is not None and int(c.get("funding_premium_multiplier", 0)) == CRYPTO_MULT:
                c = None  # одноимённая крипта на Core — не та же бумага
            rwa[m["symbol"]] = {"symbol": m["symbol"], "market_id": m["market_id"],
                                "kind": "pre-IPO" if int(m["funding_premium_multiplier"]) == 1 else "RWA",
                                "core_market_id": c["market_id"] if c else None,
                                "supported_price_decimals": m["supported_price_decimals"],
                                "supported_size_decimals": m["supported_size_decimals"]}
            if c:
                pairs[m["symbol"]] = {"rh": m["market_id"], "core": c["market_id"]}
        with self.lock:
            new_rh = sorted(rh_perp) != sorted(self.rh_markets)
            self.rh_markets = rh_perp
            self.core_markets = {m["market_id"]: m for m in core_by_sym.values()}
            self.rwa, self.pairs = rwa, pairs
        if new_rh and self.conn["trades"]["ok"]:
            self.restart["trades"] = True

    def markets_payload(self):
        """Только тикеры, которые есть и на RH, и на Core."""
        with self.lock:
            return sorted((m for m in self.rwa.values() if m["symbol"] in self.pairs), key=lambda m: m["symbol"])

    # ---------- WebSocket ----------
    async def _ws_send(self, ws, venue, msg):
        while not api.WS[venue].acquire(timeout=0):
            await asyncio.sleep(0.5)
        await ws.send(json.dumps(msg))

    def wanted_books(self, v):
        """Стаканы площадки v, нужные для хеджей: тикеры с позицией хотя бы на одной площадке."""
        with self.lock:
            held = set(self.positions["rh"]) | set(self.positions["core"])
            return {self.pairs[s][v] for s in held if s in self.pairs}

    async def book_ws(self, v):
        delay = 2
        while True:
            try:
                async with websockets.connect(api.ws_url(v), ssl=api.SSL, ping_interval=20, ping_timeout=20,
                                              max_size=None, open_timeout=15) as ws:
                    for b in self.books[v].values():
                        b.valid = False
                    await self._ws_send(ws, v, {"type": "subscribe", "channel": "market_stats/all"})
                    subbed, synced = set(), 0.0
                    self.conn[v].update(ok=True, err="")
                    delay = 2
                    while True:
                        if time.time() - synced > 2:  # позиции открылись/закрылись — без переподключения
                            synced = time.time()
                            await self._sync_subs(ws, v, subbed)
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 60)
                        except asyncio.TimeoutError:
                            raise RuntimeError("площадка молчит 60 с")
                        self.conn[v]["last"] = time.time()
                        msg = json.loads(raw)
                        kind = msg.get("type", "")
                        if kind == "ping":
                            await ws.send('{"type":"pong"}')
                            continue
                        ch = msg.get("channel", "")
                        if "order_book" in msg and ch.startswith("order_book:"):
                            mid = int(ch.split(":")[1])
                            if not self.books[v][mid].apply(msg["order_book"], kind.startswith("subscribed")):
                                # пропуск обновления: полный снимок заново (2 сообщения из бюджета)
                                await self._ws_send(ws, v, {"type": "unsubscribe", "channel": f"order_book/{mid}"})
                                await self._ws_send(ws, v, {"type": "subscribe", "channel": f"order_book/{mid}"})
                        elif "market_stats" in msg:
                            ms = msg["market_stats"]
                            rows = ms.values() if "market_id" not in ms else [ms]
                            for s in rows:
                                if isinstance(s, dict) and "market_id" in s:
                                    self.mstats[v][int(s["market_id"])] = s
                        elif kind in ("error", "shutdown"):
                            raise RuntimeError(json.dumps(msg)[:200])
            except Exception as e:
                self.conn[v]["err"] = f"{type(e).__name__}: {e}"[:200]
                self.conn[v]["reconnects"] += 1
            finally:
                self.conn[v]["ok"] = False
                for b in self.books[v].values():
                    b.valid = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _sync_subs(self, ws, v, subbed):
        want = self.wanted_books(v)
        for mid in sorted(subbed - want):
            await self._ws_send(ws, v, {"type": "unsubscribe", "channel": f"order_book/{mid}"})
            subbed.discard(mid)
            self.books[v].pop(mid, None)
        for mid in sorted(want - subbed):
            await self._ws_send(ws, v, {"type": "subscribe", "channel": f"order_book/{mid}"})
            subbed.add(mid)

    async def trades_ws(self):
        delay = 2
        while True:
            try:
                async with websockets.connect(api.ws_url("rh"), ssl=api.SSL, ping_interval=20, ping_timeout=20,
                                              max_size=None, open_timeout=15) as ws:
                    self.restart["trades"] = False
                    with self.lock:
                        mids = sorted(self.rh_markets)
                    for mid in mids:
                        await self._ws_send(ws, "rh", {"type": "subscribe", "channel": f"trade/{mid}"})
                    self.conn["trades"].update(ok=True, err="")
                    delay = 2
                    while not self.restart["trades"]:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 120)
                        except asyncio.TimeoutError:
                            raise RuntimeError("лента сделок молчит 120 с")
                        self.conn["trades"]["last"] = time.time()
                        msg = json.loads(raw)
                        if msg.get("type") == "ping":
                            await ws.send('{"type":"pong"}')
                            continue
                        trades = (msg.get("trades") or []) + (msg.get("liquidation_trades") or [])
                        if trades:
                            self.on_trades(trades)
            except Exception as e:
                self.conn["trades"]["err"] = f"{type(e).__name__}: {e}"[:200]
                self.conn["trades"]["reconnects"] += 1
            finally:
                self.conn["trades"]["ok"] = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    def on_trades(self, trades):
        self.observe(trades)
        for fn in list(self.listeners):
            try:
                fn(trades)
            except Exception as e:  # слушатель не должен ронять ленту
                self.error = f"лента: {type(e).__name__}: {e}"[:200]
        self._publish(json.dumps({"trades": trades}, separators=(",", ":")))

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def _publish(self, data):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    # ---------- позиции ----------
    def load_positions(self):
        wallet = self.wallet()
        if not wallet:
            self.set_positions(None, {})
            return
        got = {}
        for v in ("rh", "core"):
            try:
                d = api.get(v, f"account?by=l1_address&value={wallet}", timeout=20)
            except api.ApiError as e:
                if "HTTP 4" not in str(e):
                    raise
                d = {}  # на этой площадке у кошелька нет аккаунта
            got[v] = parse_accounts(d)
        if self.wallet() == wallet:  # кошелёк не сменили, пока шли запросы
            self.set_positions(wallet, got)

    def set_positions(self, wallet, got):
        with self.lock:
            for v in ("rh", "core"):
                pos, accs = got.get(v, ({}, []))
                self.positions[v] = pos
                self.accounts[v] = accs
            self.pos_wallet = wallet
            self.pos_ts, self.pos_err = (time.time() if wallet else 0.0), ""

    def wallet_changed(self):
        self.set_positions(None, {})
        self.pos_wake.set()

    def hedge_dir(self, sym):
        r, c = self.positions["rh"].get(sym), self.positions["core"].get(sym)
        if r and c and r["size"] * c["size"] < 0:
            return "rh_long" if r["size"] > 0 else "core_long"
        return None

    def quote(self, sym, qty):
        """Лучшие и средние цены на qty акций по стаканам обеих площадок; None — стакана нет."""
        p = self.pairs.get(sym)
        if not p:
            return None
        rb, cb = self.books["rh"].get(p["rh"]), self.books["core"].get(p["core"])
        if not (rb and cb and rb.valid and cb.valid):
            return None
        ra, rbid, ca, cbid = rb.levels("buy"), rb.levels("sell"), cb.levels("buy"), cb.levels("sell")
        if not (ra and rbid and ca and cbid):
            return None
        return {"mid": (ra[0][0] + rbid[0][0]) / 2, "rh_buy": fill(ra, qty), "rh_sell": fill(rbid, qty),
                "core_buy": fill(ca, qty), "core_sell": fill(cbid, qty)}

    def carry(self, sym, d):
        """Прогноз фандинга в пользу позиции, %/ч. Положительная ставка — лонги платят шортам."""
        p = self.pairs.get(sym)
        if not p:
            return None
        rate = {v: _f((self.mstats[v].get(p[v]) or {}).get("current_funding_rate")) for v in ("rh", "core")}
        if None in rate.values():
            return None
        long_v, short_v = ("rh", "core") if d == "rh_long" else ("core", "rh")
        return rate[short_v] - rate[long_v]

    def hedges(self):
        with self.lock:
            syms = sorted(set(self.positions["rh"]) | set(self.positions["core"]))
            pos = {v: dict(self.positions[v]) for v in ("rh", "core")}
        out = []
        for sym in syms:
            r, c = pos["rh"].get(sym), pos["core"].get(sym)
            h = {"symbol": sym, "rh": r, "core": c, "dir": self.hedge_dir(sym), "paired": sym in self.pairs}
            h["upnl"] = sum(x["upnl"] or 0 for x in (r, c) if x)
            # total_funding_paid_out у Lighter: >0 — получено, <0 — уплачено
            h["funding_net"] = sum(x["funding_paid"] or 0 for x in (r, c) if x)
            if h["dir"]:
                d = h["dir"]
                qty = min(abs(r["size"]), abs(c["size"]))
                h["qty"] = qty
                h["imbalance"] = abs(r["size"]) - abs(c["size"])
                base = r["entry"]
                h["entry_pct"] = 100 * ((c["entry"] - r["entry"]) if d == "rh_long" else (r["entry"] - c["entry"])) / base
                q = self.quote(sym, qty)
                if q and None not in (q["rh_sell"], q["core_buy"], q["rh_buy"], q["core_sell"]):
                    if d == "rh_long":   # закрыть: продать RH, купить Core
                        h["exit_pct"] = 100 * (q["core_buy"] - q["rh_sell"]) / q["mid"]
                        h["close_pnl"] = qty * (q["rh_sell"] - r["entry"]) + qty * (c["entry"] - q["core_buy"])
                    else:                # закрыть: купить RH, продать Core
                        h["exit_pct"] = 100 * (q["rh_buy"] - q["core_sell"]) / q["mid"]
                        h["close_pnl"] = qty * (q["core_sell"] - c["entry"]) + qty * (r["entry"] - q["rh_buy"])
                    h["profit_pct"] = h["entry_pct"] - h["exit_pct"]
                    h["notional"] = qty * q["mid"]
                h["carry_next_pct_h"] = self.carry(sym, d)
            out.append(h)
        return out

    # ---------- циклы ----------
    async def positions_loop(self):
        while True:
            self.pos_wake.clear()
            try:
                await asyncio.to_thread(self.load_positions)
            except Exception as e:
                self.pos_err = f"{type(e).__name__}: {e}"[:200]
            end = time.time() + POSITIONS_EVERY
            while time.time() < end and not self.pos_wake.is_set():
                await asyncio.sleep(0.5)

    async def markets_loop(self):
        while True:
            await asyncio.sleep(600)
            try:
                await asyncio.to_thread(self.load_markets)
            except Exception as e:
                self.error = f"рынки: {e}"[:200]

    async def saver(self):
        while True:
            await asyncio.sleep(30)
            try:
                await asyncio.to_thread(self.save_reg)
            except Exception:
                pass

    async def main(self):
        while True:
            try:
                await asyncio.to_thread(self.load_markets)
                self.error = ""
                break
            except Exception as e:
                self.error = f"рынки: {e}"[:200]
                await asyncio.sleep(15)
        await asyncio.gather(self.book_ws("rh"), self.book_ws("core"), self.trades_ws(),
                             self.positions_loop(), self.markets_loop(), self.saver())

    def start(self):
        threading.Thread(target=lambda: asyncio.run(self.main()), daemon=True, name="market").start()

    def status(self):
        now = time.time()
        return {"conn": {k: {"ok": c["ok"], "age": round(now - c["last"], 1) if c["last"] else None,
                             "err": c["err"], "reconnects": c["reconnects"]} for k, c in self.conn.items()},
                "limits": api.stats(), "positions_age": round(now - self.pos_ts) if self.pos_ts else None,
                "positions_err": self.pos_err, "error": self.error, "pairs": len(self.pairs),
                "registry": len(self.reg)}


def parse_accounts(d):
    """Ответ /account?by=l1_address -> (позиции по тикерам, счета)."""
    pos, accs = {}, []
    for a in d.get("accounts") or []:
        accs.append({"index": a["index"], "collateral": _f(a.get("collateral")),
                     "total": _f(a.get("total_asset_value")), "available": _f(a.get("available_balance")),
                     "im": _f(a.get("cross_initial_margin_requirement")),
                     "mm": _f(a.get("cross_maintenance_margin_requirement"))})
        for p in a.get("positions") or []:
            size = float(p.get("position") or 0) * (1 if p.get("sign", 1) >= 0 else -1)
            if abs(size) < 1e-12:
                continue
            pos[p["symbol"]] = {"size": size, "entry": float(p["avg_entry_price"]), "upnl": _f(p.get("unrealized_pnl")),
                                "mark": (_f(p.get("position_value")) or 0) / abs(size),
                                "funding_paid": _f(p.get("total_funding_paid_out")), "liq": _f(p.get("liquidation_price")),
                                "value": _f(p.get("position_value")), "account": a["index"]}
    return pos, accs
