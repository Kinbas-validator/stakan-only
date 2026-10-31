"""Аккаунт по кошельку: сделки на Lighter RH и с кем они прошли — с premium или нет.

Кошелёк задаётся на странице (или --wallet при запуске) и хранится в data/config.json.
По адресу находится основной аккаунт Robinhood Chain (account_type 0).

Сделки аккаунта — REST /trades (в фоне, раз в TRADES_EVERY с), в data/accounts/<индекс>/trades.jsonl.
Комиссию контрагента лента аккаунта не отдаёт, поэтому она берётся:
  * для новых сделок — сразу из публичной WS-ленты сделок (там видны комиссии обеих сторон);
  * для сделок, прошедших без запущенного сервера, — из транзакции /tx (event_info: tf — тейкер, mf — мейкер),
    в фоновом бюджете запросов. Только для текущей недели и последних MY_TRADES сделок.
Классы контрагента по его комиссии (единицы 1e-6, 120 = 0.012%): 0 — standard, 50 — plus, больше — premium.

Неделя очков: пятница 15:00 → пятница 15:00 по Турции (UTC+3).
"""
import datetime as dt
import json
import os
import re
import threading
import time
from collections import defaultdict

import lighter_api as api
from market import tier_of_fee

ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
TR = dt.timezone(dt.timedelta(hours=3))
ANCHOR = dt.datetime(2026, 10, 9, 15, 0, tzinfo=TR)   # опорная граница недели очков
WEEK = dt.timedelta(days=7)
TRADES_EVERY = 20   # с между проверками новых сделок
TX_EVERY = 2.5      # с между запросами комиссий из транзакций (фоновый бюджет)
MY_TRADES = 150     # сколько последних сделок отдавать странице


class WalletError(Exception):
    pass


def week_of(ts_ms):
    t = dt.datetime.fromtimestamp(ts_ms / 1000, TR)
    start = ANCHOR + ((t - ANCHOR) // WEEK) * WEEK
    return start, start + WEEK


def read_jsonl(path):
    rows = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:  # недописанная строка
                    pass
    return rows


def classify(trades, fees, me):
    """Сделки аккаунта me + комиссии -> строки: сторона, роль, контрагент и его тариф. Повторы по trade_id отбрасываются."""
    rows = []
    for t in sorted({t["trade_id"]: t for t in trades}.values(), key=lambda t: t["timestamp"]):
        f = fees.get(t["trade_id"])
        i_am_ask = t["ask_account_id"] == me
        role = "maker" if i_am_ask == t["is_maker_ask"] else "taker"
        if f is None:
            cp_fee = None
        else:
            cp_fee = f["tf"] if role == "maker" else f["mf"]
        start, _ = week_of(t["timestamp"])
        rows.append({
            "trade_id": t["trade_id"], "ts": t["timestamp"], "week_start": start.isoformat(),
            "symbol": t.get("market_symbol"), "side": "sell" if i_am_ask else "buy", "my_role": role,
            "size": t["size"], "price": t["price"], "usd": float(t["usd_amount"]),
            "counterparty": t["bid_account_id"] if i_am_ask else t["ask_account_id"],
            "cp_fee": cp_fee, "cp_tier": "unknown" if f is None else tier_of_fee(cp_fee),
            "my_order": t["ask_id"] if i_am_ask else t["bid_id"],
        })
    return rows


def agg(rows):
    """Счётчики по количеству и объёму в разрезе классов контрагента."""
    a = {"trades": 0, "usd": 0.0}
    for tr in ("premium", "plus", "standard", "unknown"):
        a[f"{tr}_n"] = 0
        a[f"{tr}_usd"] = 0.0
    for r in rows:
        a["trades"] += 1
        a["usd"] += r["usd"]
        a[f"{r['cp_tier']}_n"] += 1
        a[f"{r['cp_tier']}_usd"] += r["usd"]
    a["premium_pct_n"] = 100 * a["premium_n"] / a["trades"] if a["trades"] else 0
    a["premium_pct_usd"] = 100 * a["premium_usd"] / a["usd"] if a["usd"] else 0
    return a


def resolve(wallet):
    """Адрес кошелька -> основной аккаунт на Robinhood Chain."""
    if not ADDR.match(wallet or ""):
        raise WalletError("адрес кошелька — 0x и 40 шестнадцатеричных символов")
    try:
        d = api.get("rh", f"account?by=l1_address&value={wallet}", timeout=20)
    except api.ApiError as e:
        if "HTTP 4" in str(e):
            raise WalletError("на Robinhood Chain нет аккаунта для этого кошелька") from e
        raise WalletError(f"Lighter не ответил: {e}") from e
    accs = d.get("accounts") or []
    if not accs:
        raise WalletError("на Robinhood Chain нет аккаунта для этого кошелька")
    main = min(accs, key=lambda a: (a.get("account_type", 0) != 0, a["index"]))
    return {"wallet": wallet, "account": int(main["index"]), "sub_accounts": sorted(int(a["index"]) for a in accs)}


class Tracker:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.cfg_path = os.path.join(data_dir, "config.json")
        self.lock = threading.Lock()
        self.cfg = self._load_cfg()
        self.state = {"pending": 0, "last_poll": 0, "error": ""}
        self.have_fee = None   # trade_id с известной комиссией (для текущего аккаунта)
        self.wake = threading.Event()
        self.on_change = []    # кто ещё должен знать о смене кошелька

    # ---------- кошелёк ----------
    def _load_cfg(self):
        try:
            with open(self.cfg_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def wallet(self):
        return self.cfg.get("wallet")

    def account(self):
        return self.cfg.get("account")

    def set_wallet(self, wallet):
        info = resolve((wallet or "").strip())
        with self.lock:
            self.cfg = {**self.cfg, **info}
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = self.cfg_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.cfg, f, indent=1)
            os.replace(tmp, self.cfg_path)
            self.have_fee = None
            self.state = {"pending": 0, "last_poll": 0, "error": ""}
        for fn in self.on_change:
            fn()
        self.wake.set()
        return self.info()

    def info(self):
        return {k: self.cfg.get(k) for k in ("wallet", "account", "sub_accounts")}

    # ---------- файлы аккаунта ----------
    def paths(self, account):
        d = os.path.join(self.data_dir, "accounts", str(account))
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "trades.jsonl"), os.path.join(d, "tx_fees.jsonl")

    def _append(self, path, recs):
        with open(path, "a") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def observe_live(self, trades):
        """Публичная лента: комиссии моих новых сделок видны сразу — запрос /tx для них не нужен."""
        me = self.account()
        if me is None:
            return
        recs = []
        for t in trades:
            if me not in (t.get("ask_account_id"), t.get("bid_account_id")):
                continue
            recs.append({"trade_id": t["trade_id"], "tf": t.get("taker_fee") or 0, "mf": t.get("maker_fee") or 0,
                         "tx_type": t.get("type"), "src": "ws"})
        if not recs:
            return
        with self.lock:
            if self.account() != me:
                return
            _, fees = self.paths(me)
            if self.have_fee is not None:
                recs = [r for r in recs if r["trade_id"] not in self.have_fee]
                self.have_fee.update(r["trade_id"] for r in recs)
            self._append(fees, recs)

    # ---------- докачка ----------
    def fetch_new_trades(self, account, known, since_ms):
        """Новые сделки (свежие сверху): до первой уже известной; при первом запуске — до начала недели."""
        new, cursor = [], ""
        for _ in range(100):
            path = f"trades?account_index={account}&sort_by=timestamp&sort_dir=desc&limit=100"
            if cursor:
                path += f"&cursor={cursor}"
            page = api.get("rh", path)
            trades = page.get("trades") or []
            fresh = [t for t in trades if t["trade_id"] not in known]
            new += fresh
            cursor = page.get("next_cursor")
            if len(fresh) < len(trades) or not cursor or not trades:
                break
            if not known and trades[-1]["timestamp"] < since_ms and len(new) >= MY_TRADES:
                break
        return new

    @staticmethod
    def fetch_fee(t):
        tx = api.get("rh", f"tx?by=hash&value={t['tx_hash']}", low=True)
        ev = json.loads(tx.get("event_info") or "{}")
        tr = ev.get("t") or {}
        return {"trade_id": t["trade_id"], "tx_type": tx.get("type"), "tf": tr.get("tf"), "mf": tr.get("mf"), "src": "tx"}

    def step(self):
        """Одна итерация: новые сделки + комиссии для них (свежие — первыми). Возвращает время следующей проверки."""
        deadline = time.time() + TRADES_EVERY
        me = self.account()
        if me is None:
            return deadline
        trades_path, fees_path = self.paths(me)
        with self.lock:
            trades = read_jsonl(trades_path)
            fee_ids = {f["trade_id"] for f in read_jsonl(fees_path)}
            if self.have_fee is None:
                self.have_fee = fee_ids
            have = self.have_fee   # своя ссылка: смена кошелька обнулит self.have_fee
        known = {t["trade_id"] for t in trades}
        week_start, _ = week_of(time.time() * 1000)
        since = int(week_start.timestamp() * 1000)
        new = self.fetch_new_trades(me, known, since)
        if self.account() != me:
            return deadline
        with self.lock:
            self._append(trades_path, new)
        trades += new
        self.state["last_poll"] = time.time()
        recent = sorted({t["trade_id"]: t for t in trades}.values(), key=lambda t: -t["timestamp"])
        todo = [t for i, t in enumerate(recent) if (t["timestamp"] >= since or i < MY_TRADES) and t["trade_id"] not in have]
        self.state["pending"] = len(todo)
        for t in todo:
            if time.time() > deadline or self.wake.is_set() or self.account() != me:
                break
            if t["trade_id"] in have:  # пока ждали, пришла из живой ленты
                self.state["pending"] -= 1
                continue
            rec = self.fetch_fee(t)
            with self.lock:
                if self.account() == me and t["trade_id"] not in have:
                    have.add(t["trade_id"])
                    self._append(fees_path, [rec])
            self.state["pending"] -= 1
            time.sleep(TX_EVERY)
        self.state["error"] = ""
        return deadline

    def loop(self):
        while True:
            self.wake.clear()
            if self.account() is None:
                self.wake.wait(5)
                continue
            try:
                self.wake.wait(max(0.0, self.step() - time.time()))
            except Exception as e:
                self.state["error"] = f"{type(e).__name__}: {e}"[:200]
                self.wake.wait(15)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="tracker").start()

    # ---------- ответ для страницы ----------
    def payload(self):
        me = self.account()
        week_start, week_end = week_of(time.time() * 1000)
        out = {**self.info(), "week": {"label": f"{week_start:%d.%m %H:%M} – {week_end:%d.%m %H:%M}",
                                       "start_ms": int(week_start.timestamp() * 1000), **agg([])},
               "by_symbol": [], "trades": [], **self.state}
        if me is None:
            return out
        trades_path, fees_path = self.paths(me)
        with self.lock:
            trades = read_jsonl(trades_path)
            fees = {f["trade_id"]: f for f in read_jsonl(fees_path)}
        rows = classify(trades, fees, me)
        week = [r for r in rows if r["week_start"] == week_start.isoformat()]
        by_sym = defaultdict(list)
        for r in week:
            by_sym[r["symbol"]].append(r)
        out["week"].update(agg(week))
        out["by_symbol"] = sorted(({"symbol": s, **agg(v)} for s, v in by_sym.items()), key=lambda a: -a["usd"])
        out["trades"] = rows[::-1][:MY_TRADES]
        return out
