"""Стакан Lighter RH с пометкой premium-ордеров: локальный веб-сервер.

Запуск: .venv/bin/python server.py [--port 8765] [--no-browser] [--wallet 0x...]
Откроется http://127.0.0.1:8765. Остановить — Ctrl+C. Кошелёк можно задать и на странице.

Сервер:
  * отдаёт страницу из web/;
  * держит ленту сделок RH (реестр тарифов premium/standard), позиции и счета по кошельку (market.py);
  * докачивает сделки аккаунта кошелька и комиссии контрагентов (account.py);
  * все REST-запросы к Lighter — свои и страницы — идут через общий бюджет lighter_api.py.
Страница сама держит одно WS-соединение со стаканом выбранного тикера RH.
"""
import argparse
import json
import os
import queue
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import account
import lighter_api as api
import market

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
DATA = os.environ.get("STAKAN_DATA") or os.path.join(ROOT, "data")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/book.js": ("book.js", "text/javascript; charset=utf-8")}
OWNERS_TTL = 2.0   # с: владельцы ордеров кешируются для всех вкладок
RECENT_TTL = 30.0


class App:
    """Всё состояние сервера: рынки, аккаунт и кеши запросов страницы."""
    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.tracker = account.Tracker(data_dir)
        self.market = market.Market(data_dir, wallet=self.tracker.wallet)
        self.market.listeners.append(self.tracker.observe_live)
        self.tracker.on_change.append(self.market.wallet_changed)
        self.cache, self.locks = {}, defaultdict(threading.Lock)

    def start(self):
        self.market.start()
        self.tracker.start()

    def cached(self, key, ttl, path):
        """Один запрос к Lighter на ttl секунд для всех вкладок."""
        with self.locks[key]:
            hit = self.cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
            d = api.get("rh", path, timeout=8, tries=1)
            self.cache[key] = (time.time(), d)
            return d

    def owners(self, mid):
        return self.cached(("owners", mid), OWNERS_TTL, f"orderBookOrders?market_id={mid}&limit=100")

    def recent(self, mid):
        return self.cached(("recent", mid), RECENT_TTL, f"recentTrades?market_id={mid}&limit=100")

    def hedges(self):
        m = self.market
        with m.lock:  # позиции прошлого кошелька, пока не загрузились новые, не показываем
            fresh = m.pos_wallet == self.tracker.wallet()
            accounts = dict(m.accounts) if fresh else {}
        return {"wallet": self.tracker.wallet(), "hedges": m.hedges() if fresh else [], "accounts": accounts,
                "status": m.status()}

    def status(self):
        return {**self.market.status(), "wallet": self.tracker.info(), "tracker": self.tracker.state}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False, allow_nan=False, default=str).encode(),
                       "application/json; charset=utf-8")

        def _int(self, qs, key):
            try:
                return int(qs.get(key, [""])[0])
            except ValueError:
                return None

        def do_GET(self):
            u = urlsplit(self.path)
            path, qs = u.path, parse_qs(u.query)
            try:
                if path in STATIC:
                    name, ctype = STATIC[path]
                    with open(os.path.join(WEB, name), "rb") as f:
                        self._send(200, f.read(), ctype)
                elif path == "/markets":
                    self._json(app.market.markets_payload())
                elif path == "/registry":
                    self._json(app.market.reg_copy())
                elif path in ("/owners", "/recent"):
                    mid = self._int(qs, "market_id")
                    if mid is None:
                        return self._json({"error": "нужен market_id"}, 400)
                    self._json(app.owners(mid) if path == "/owners" else app.recent(mid))
                elif path == "/my":
                    self._json(app.tracker.payload())
                elif path == "/hedges":
                    self._json(app.hedges())
                elif path == "/wallet":
                    self._json(app.tracker.info())
                elif path == "/status":
                    self._json(app.status())
                elif path == "/events":
                    self._events()
                else:
                    self._send(404, b"not found", "text/plain")
            except api.ApiError as e:
                self._json({"error": str(e)}, 503)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _events(self):
            """SSE: живые сделки RH (все рынки) — из одного соединения сервера с биржей."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = app.market.subscribe()
            try:
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
                while True:
                    try:
                        data = q.get(timeout=15)
                        self.wfile.write(b"data: " + data.encode() + b"\n\n")
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.market.unsubscribe(q)

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except ValueError:
                return self._json({"error": "неверный JSON"}, 400)
            if path == "/wallet":
                try:
                    self._json(app.tracker.set_wallet(str(body.get("wallet", ""))))
                except account.WalletError as e:
                    self._json({"error": str(e)}, 400)
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *args):  # без шума в терминале
            pass

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Стакан Lighter RH с пометкой premium-ордеров")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="адрес; наружу не открывать — доступ через SSH-туннель")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--wallet", help="адрес кошелька 0x… (можно задать и на странице)")
    a = ap.parse_args(argv)
    app = App(DATA)
    if a.wallet:
        try:
            app.tracker.set_wallet(a.wallet)
        except account.WalletError as e:
            print(f"Кошелёк не принят: {e}", file=sys.stderr)
            return 2
    srv = Server((a.host, a.port), make_handler(app))
    app.start()
    url = f"http://127.0.0.1:{a.port}"
    w = app.tracker.info()
    who = f"кошелёк {w['wallet']}, аккаунт RH {w['account']}" if w["wallet"] else "кошелёк не задан — укажите его на странице"
    print(f"Стакан запущен: {url}  ({who}; остановить — Ctrl+C)", flush=True)
    if not a.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.market.save_reg()


if __name__ == "__main__":
    sys.exit(main())
